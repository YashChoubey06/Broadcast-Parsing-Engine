"""
ml_classifier.py
================
Layer 2 of the hybrid parser: scikit-learn action classifier.

Architecture
-----------
  FeatureUnion:
    - TfidfVectorizer (word, ngram 1-2)
    - TfidfVectorizer (char_wb, ngram 3-5)
  Classifier: LogisticRegression (primary, provides predict_proba)
  Alternatives: ComplementNB, LinearSVC (with CalibratedClassifierCV for proba)

Training data: 03_model_train_ready_deduplicated.csv (or pilot files)
Target column: action_label
Feature column: normalized_text

IMPORTANT NOTES:
 - The NLP model never modifies holdings.
 - Rule-based actions override ML for deterministic phrases.
 - Classes with < MIN_ML_CLASS_SAMPLES training examples are unreliable.
 - Labels are initial/weak labels (rule-assisted), not fully human-verified.
 - The pilot test set must NOT be used for tuning – only for final evaluation.
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
)
from sklearn.naive_bayes import ComplementNB
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.svm import LinearSVC

from src.config import (
    AUTO_APPLY_THRESHOLD,
    MIN_ML_CLASS_SAMPLES,
    MODEL_METADATA_PATH,
    MODEL_PATH,
    MODELS_DIR,
    PARSER_VERSION,
    RANDOM_STATE,
    REPORTS_DIR,
    REVIEW_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Feature union (shared across all classifiers)
# ---------------------------------------------------------------------------
def _build_feature_union() -> FeatureUnion:
    word_tfidf = TfidfVectorizer(
        ngram_range=(1, 2),
        lowercase=True,
        min_df=1,
        sublinear_tf=True,
        analyzer="word",
    )
    char_tfidf = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(3, 5),
        min_df=1,
        sublinear_tf=True,
    )
    return FeatureUnion([("word", word_tfidf), ("char", char_tfidf)])


def build_lr_pipeline() -> Pipeline:
    """Logistic Regression pipeline (default production classifier)."""
    return Pipeline([
        ("features", _build_feature_union()),
        ("clf", LogisticRegression(
            max_iter=3000,
            class_weight="balanced",
            random_state=RANDOM_STATE,
            solver="saga",
            C=1.0,
        )),
    ])


def build_cnb_pipeline() -> Pipeline:
    """ComplementNB pipeline (comparison baseline)."""
    return Pipeline([
        ("features", _build_feature_union()),
        ("clf", ComplementNB()),
    ])


def build_lsvc_pipeline() -> Pipeline:
    """
    LinearSVC wrapped with CalibratedClassifierCV so predict_proba is available.
    Note: raw LinearSVC decision scores are NOT probabilities.
    """
    lsvc = LinearSVC(random_state=RANDOM_STATE, max_iter=3000, class_weight="balanced")
    calibrated = CalibratedClassifierCV(lsvc, cv=3, method="sigmoid")
    return Pipeline([
        ("features", _build_feature_union()),
        ("clf", calibrated),
    ])


# ---------------------------------------------------------------------------
# Hash-safe group split (prevents leakage)
# ---------------------------------------------------------------------------
def group_split_by_hash(
    df: pd.DataFrame,
    test_size: float = 0.15,
    val_size: float = 0.15,
    hash_col: str = "normalized_text_hash",
    random_state: int = RANDOM_STATE,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Split a DataFrame into train / validation / test without leaking rows
    that share the same text hash into multiple sets.
    """
    rng = np.random.default_rng(random_state)
    unique_hashes = df[hash_col].dropna().unique()
    rng.shuffle(unique_hashes)

    n = len(unique_hashes)
    n_test = max(1, int(n * test_size))
    n_val = max(1, int(n * val_size))

    test_hashes = set(unique_hashes[:n_test])
    val_hashes = set(unique_hashes[n_test: n_test + n_val])
    train_hashes = set(unique_hashes[n_test + n_val:])

    train = df[df[hash_col].isin(train_hashes)].copy()
    val = df[df[hash_col].isin(val_hashes)].copy()
    test = df[df[hash_col].isin(test_hashes)].copy()

    return train, val, test


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------
def _compute_metrics(y_true, y_pred, y_proba, classes, split_name: str) -> dict:
    """Compute comprehensive evaluation metrics."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        report = classification_report(
            y_true, y_pred, labels=classes, output_dict=True, zero_division=0
        )

    # Per-class support
    support = {c: int(np.sum(np.array(y_true) == c)) for c in classes}

    # Low-confidence predictions (only available when proba is not None)
    low_conf_rows = []
    if y_proba is not None:
        max_proba = y_proba.max(axis=1)
        for i, (yt, yp, conf) in enumerate(zip(y_true, y_pred, max_proba)):
            if conf < AUTO_APPLY_THRESHOLD:
                low_conf_rows.append({
                    "index": i,
                    "true_label": yt,
                    "predicted_label": yp,
                    "confidence": round(float(conf), 4),
                })

    return {
        "split": split_name,
        "accuracy": round(accuracy_score(y_true, y_pred), 4),
        "macro_f1": round(f1_score(y_true, y_pred, average="macro", zero_division=0), 4),
        "weighted_f1": round(f1_score(y_true, y_pred, average="weighted", zero_division=0), 4),
        "per_class": {
            c: {
                "precision": round(report.get(c, {}).get("precision", 0.0), 4),
                "recall": round(report.get(c, {}).get("recall", 0.0), 4),
                "f1": round(report.get(c, {}).get("f1-score", 0.0), 4),
                "support": support.get(c, 0),
            }
            for c in classes
        },
        "low_confidence_count": len(low_conf_rows),
        "low_confidence_items": low_conf_rows[:50],  # cap for report size
    }


def save_reports(
    pipeline,
    X_test,
    y_test,
    classes: list[str],
    split_name: str = "test",
    X_val=None,
    y_val=None,
    misclassified_df: Optional[pd.DataFrame] = None,
):
    """Save evaluation reports to reports/."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    y_pred = pipeline.predict(X_test)
    y_proba = None
    if hasattr(pipeline, "predict_proba"):
        try:
            y_proba = pipeline.predict_proba(X_test)
        except Exception:
            pass

    metrics = _compute_metrics(y_test, y_pred, y_proba, classes, split_name)

    # JSON report
    report_json = REPORTS_DIR / "classification_report.json"
    with open(report_json, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    # CSV classification report
    rows = []
    for cls, vals in metrics["per_class"].items():
        rows.append({"class": cls, **vals})
    pd.DataFrame(rows).to_csv(REPORTS_DIR / "classification_report.csv", index=False)

    # Confusion matrix
    cm = confusion_matrix(y_test, y_pred, labels=classes)
    cm_df = pd.DataFrame(cm, index=classes, columns=classes)
    cm_df.to_csv(REPORTS_DIR / "confusion_matrix.csv")

    # Misclassified examples
    y_pred_arr = np.array(y_pred)
    y_test_arr = np.array(y_test)
    misclassified_idx = np.where(y_pred_arr != y_test_arr)[0]
    if misclassified_df is not None and len(misclassified_idx):
        mis_rows = misclassified_df.iloc[misclassified_idx].copy()
        mis_rows["predicted"] = y_pred_arr[misclassified_idx]
        mis_rows.to_csv(REPORTS_DIR / "misclassified_examples.csv", index=False)

    # Low-confidence predictions
    if metrics["low_confidence_items"]:
        pd.DataFrame(metrics["low_confidence_items"]).to_csv(
            REPORTS_DIR / "low_confidence_predictions.csv", index=False
        )

    return metrics


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------
def save_model(pipeline, metadata: dict) -> None:
    """Persist the trained pipeline and metadata."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, MODEL_PATH)
    with open(MODEL_METADATA_PATH, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    print(f"Model saved -> {MODEL_PATH}")
    print(f"Metadata saved -> {MODEL_METADATA_PATH}")


def load_model():
    """
    Load the trained pipeline from disk.

    Raises a clear RuntimeError if the model is missing rather than
    producing an unclear traceback.
    """
    if not MODEL_PATH.exists():
        raise RuntimeError(
            f"\n{'='*60}\n"
            "Model file not found:\n"
            f"  {MODEL_PATH}\n\n"
            "Train the model first:\n"
            "  python -m src.train_model "
            "--train data/07_pilot_train_210.csv "
            "--validation data/08_pilot_validation_45.csv "
            "--test data/09_pilot_test_45.csv\n"
            f"{'='*60}\n"
        )
    return joblib.load(MODEL_PATH)


def load_model_metadata() -> dict:
    """Load model metadata if available."""
    if MODEL_METADATA_PATH.exists():
        with open(MODEL_METADATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ---------------------------------------------------------------------------
# Prediction interface
# ---------------------------------------------------------------------------
class MLClassifier:
    """
    Thin wrapper around the trained scikit-learn pipeline.

    Usage::

        clf = MLClassifier()
        action, confidence = clf.predict("50% profit book in NIFTY")
    """

    def __init__(self, pipeline=None) -> None:
        self._pipeline = pipeline
        self._classes: list[str] = []
        if pipeline is not None:
            self._classes = list(pipeline.classes_) if hasattr(pipeline, "classes_") else []

    @classmethod
    def from_file(cls) -> "MLClassifier":
        pipeline = load_model()
        inst = cls(pipeline)
        return inst

    def predict(self, text: str) -> tuple[str, float]:
        """
        Return (predicted_action, confidence).

        confidence is the max class probability from LogisticRegression.
        Returns ("UNKNOWN", 0.0) if the model is not loaded.
        """
        if self._pipeline is None:
            return "UNKNOWN", 0.0
        try:
            if hasattr(self._pipeline, "predict_proba"):
                proba = self._pipeline.predict_proba([text])[0]
                idx = int(np.argmax(proba))
                classes = self._pipeline.classes_
                return str(classes[idx]), float(proba[idx])
            else:
                pred = self._pipeline.predict([text])[0]
                return str(pred), 0.0
        except Exception:
            return "UNKNOWN", 0.0

    def is_loaded(self) -> bool:
        return self._pipeline is not None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_default_clf: Optional[MLClassifier] = None


def get_classifier() -> MLClassifier:
    """Return the cached MLClassifier, loading from disk on first call."""
    global _default_clf
    if _default_clf is None:
        try:
            _default_clf = MLClassifier.from_file()
        except RuntimeError:
            # Return an unloaded classifier; the caller can check is_loaded()
            _default_clf = MLClassifier()
    return _default_clf
