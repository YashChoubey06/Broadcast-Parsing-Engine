"""
train_model.py
==============
CLI: Train the scikit-learn action classifier.

Usage examples
--------------
Pilot train/val/test split:
    python -m src.train_model \\
        --train data/07_pilot_train_210.csv \\
        --validation data/08_pilot_validation_45.csv \\
        --test data/09_pilot_test_45.csv

Full dataset with automatic hash-safe split:
    python -m src.train_model \\
        --train data/03_model_train_ready_deduplicated.csv \\
        --auto-split

NOTES:
 - Labels are initial/weak (rule-assisted), not fully human-verified.
 - Pilot test set is NEVER used for tuning – only final untouched evaluation.
 - Classes with < MIN_ML_CLASS_SAMPLES examples are flagged in the report.
 - LinearSVC uses calibrated probabilities, not raw decision values.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn

from src.config import (
    DATA_DIR,
    LABEL_COL,
    TEXT_COL,
    HASH_COL,
    MIN_ML_CLASS_SAMPLES,
    MODEL_METADATA_PATH,
    MODEL_PATH,
    MODELS_DIR,
    PARSER_VERSION,
    PILOT_TEST_FILE,
    PILOT_TRAIN_FILE,
    PILOT_VALIDATION_FILE,
    RANDOM_STATE,
    REPORTS_DIR,
    TRAIN_READY_FILE,
    resolve_input_path,
)
from src.ml_classifier import (
    MLClassifier,
    build_cnb_pipeline,
    build_lr_pipeline,
    build_lsvc_pipeline,
    group_split_by_hash,
    save_model,
    save_reports,
)

def _load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    # Drop rows without text or label
    df = df.dropna(subset=[TEXT_COL, LABEL_COL])
    df[TEXT_COL] = df[TEXT_COL].astype(str).str.strip()
    df = df[df[TEXT_COL] != ""]
    return df


def _flag_small_classes(df: pd.DataFrame) -> None:
    counts = df[LABEL_COL].value_counts()
    small = counts[counts < MIN_ML_CLASS_SAMPLES]
    if not small.empty:
        print(
            f"\n[WARNING] Classes with fewer than {MIN_ML_CLASS_SAMPLES} training examples "
            "(rely on deterministic rules, not ML):"
        )
        for cls, cnt in small.items():
            print(f"   {cls}: {cnt} examples")


def _print_metrics(metrics: dict) -> None:
    print(f"\n{'='*55}")
    print(f"Split: {metrics.get('split', '?').upper()}")
    print(f"  Accuracy:    {metrics['accuracy']:.4f}")
    print(f"  Macro F1:    {metrics['macro_f1']:.4f}")
    print(f"  Weighted F1: {metrics['weighted_f1']:.4f}")
    print(f"  Low-confidence predictions: {metrics.get('low_confidence_count', 0)}")
    print(f"\n  Per-class (support = # examples in this split):")
    for cls, v in sorted(metrics["per_class"].items()):
        print(
            f"    {cls:<30} P={v['precision']:.3f} R={v['recall']:.3f} "
            f"F1={v['f1']:.3f} support={v['support']}"
        )
    print("=" * 55)


def train(args: argparse.Namespace) -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    train_path = resolve_input_path(args.train)

    print(f"Loading training data from: {train_path}")
    train_df = _load_csv(train_path)
    print(f"  Rows loaded: {len(train_df)}")

    val_df: pd.DataFrame | None = None
    test_df: pd.DataFrame | None = None

    if args.auto_split:
        # Hash-safe group split to prevent leakage
        if HASH_COL not in train_df.columns:
            train_df[HASH_COL] = train_df[TEXT_COL].apply(
                lambda t: __import__("hashlib").md5(t.encode()).hexdigest()[:16]
            )
        train_df, val_df, test_df = group_split_by_hash(train_df)
        print(
            f"  Auto-split → train={len(train_df)} val={len(val_df)} test={len(test_df)}"
        )
    else:
        if args.validation:
            val_path = resolve_input_path(args.validation)
            print(f"Loading validation data from: {val_path}")
            val_df = _load_csv(val_path)
            print(f"  Rows loaded: {len(val_df)}")
        if args.test:
            test_path = resolve_input_path(args.test)
            print(f"Loading test data from: {test_path}")
            test_df = _load_csv(test_path)
            print(f"  Rows loaded: {len(test_df)}")

    # Flag small classes
    _flag_small_classes(train_df)

    X_train = train_df[TEXT_COL].tolist()
    y_train = train_df[LABEL_COL].tolist()
    classes = sorted(set(y_train))

    print(f"\nClasses ({len(classes)}): {classes}")

    # ---- Train LogisticRegression (default) --------------------------------
    print("\nTraining LogisticRegression pipeline …")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        lr_pipeline = build_lr_pipeline()
        lr_pipeline.fit(X_train, y_train)

    val_metrics = None
    test_metrics = None

    # ---- Validation evaluation --------------------------------------------
    if val_df is not None and len(val_df):
        X_val = val_df[TEXT_COL].tolist()
        y_val = val_df[LABEL_COL].tolist()
        all_cls = sorted(set(classes) | set(y_val))
        val_metrics = save_reports(
            lr_pipeline, X_val, y_val, all_cls, "validation", misclassified_df=val_df
        )
        _print_metrics(val_metrics)

    # ---- Final test evaluation (MUST NOT be used for tuning) --------------
    if test_df is not None and len(test_df):
        print("\n[WARNING] Final test-set evaluation (do not tune on this result):")
        X_test = test_df[TEXT_COL].tolist()
        y_test = test_df[LABEL_COL].tolist()
        all_cls = sorted(set(classes) | set(y_test))
        test_metrics = save_reports(
            lr_pipeline, X_test, y_test, all_cls, "test", misclassified_df=test_df
        )
        _print_metrics(test_metrics)

    # ---- Optional comparison models (ComplementNB, LinearSVC) -------------
    comparison_results = {}
    if args.compare:
        print("\nComparing with ComplementNB …")
        try:
            cnb = build_cnb_pipeline()
            cnb.fit(X_train, y_train)
            if val_df is not None:
                y_pred_cnb = cnb.predict(val_df[TEXT_COL].tolist())
                from sklearn.metrics import f1_score as _f1
                comparison_results["ComplementNB_macro_f1"] = round(
                    _f1(val_df[LABEL_COL].tolist(), y_pred_cnb, average="macro", zero_division=0), 4
                )
                print(f"  ComplementNB macro F1 (val): {comparison_results['ComplementNB_macro_f1']:.4f}")
        except Exception as e:
            print(f"  ComplementNB comparison failed: {e}")

        print("Comparing with LinearSVC (calibrated) …")
        try:
            lsvc = build_lsvc_pipeline()
            lsvc.fit(X_train, y_train)
            if val_df is not None:
                y_pred_lsvc = lsvc.predict(val_df[TEXT_COL].tolist())
                from sklearn.metrics import f1_score as _f1
                comparison_results["LinearSVC_calibrated_macro_f1"] = round(
                    _f1(val_df[LABEL_COL].tolist(), y_pred_lsvc, average="macro", zero_division=0), 4
                )
                print(
                    f"  LinearSVC macro F1 (val): {comparison_results['LinearSVC_calibrated_macro_f1']:.4f}"
                )
        except Exception as e:
            print(f"  LinearSVC comparison failed: {e}")

    # ---- Save model --------------------------------------------------------
    metadata = {
        "model_version": "1.0.0",
        "training_timestamp": datetime.now(timezone.utc).isoformat(),
        "training_file": str(train_path),
        "training_row_count": len(train_df),
        "class_labels": classes,
        "scikit_learn_version": sklearn.__version__,
        "python_version": platform.python_version(),
        "validation_metrics": val_metrics,
        "test_metrics": test_metrics,
        "comparison_metrics": comparison_results,
        "parser_version": PARSER_VERSION,
        "label_note": (
            "Labels are initial/weak (rule-assisted annotation). "
            "Not fully human-verified gold-standard. "
            "Evaluation represents baseline performance."
        ),
        "pilot_test_is_untouched_baseline": True,
    }
    save_model(lr_pipeline, metadata)
    print(f"\nReports saved to: {REPORTS_DIR}/")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train the trade-message action classifier.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--train", default=PILOT_TRAIN_FILE,
        help=f"Training CSV file (relative to data/ or absolute). Default: {PILOT_TRAIN_FILE}"
    )
    parser.add_argument(
        "--validation", default=None,
        help="Validation CSV file."
    )
    parser.add_argument(
        "--test", default=None,
        help="Test CSV file (never used for tuning)."
    )
    parser.add_argument(
        "--auto-split", action="store_true",
        help="Hash-safe auto-split of the training file (ignores --validation/--test)."
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="Also train ComplementNB and LinearSVC for comparison."
    )
    args = parser.parse_args()
    train(args)


if __name__ == "__main__":
    main()
