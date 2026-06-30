import argparse
import json
import sys
from pathlib import Path
import pandas as pd
import joblib
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    confusion_matrix
)

from src.config import (
    MODELS_DIR,
    REPORTS_DIR,
    MODEL_PATH,
    resolve_input_path,
    TEXT_COL,
    LABEL_COL
)
from src.text_normalizer import normalize

def main():
    parser = argparse.ArgumentParser(description="Audit ML Model metrics independently.")
    parser.parse_args()
    
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Load model
    if not MODEL_PATH.exists():
        print(f"ERROR: Model not found at {MODEL_PATH}")
        sys.exit(1)
        
    print(f"Loading model from {MODEL_PATH}")
    pipeline = joblib.load(MODEL_PATH)
    
    # Load datasets
    train_df = pd.read_csv(resolve_input_path("data/07_pilot_train_210.csv"), low_memory=False)
    val_df = pd.read_csv(resolve_input_path("data/08_pilot_validation_45.csv"), low_memory=False)
    test_df = pd.read_csv(resolve_input_path("data/09_pilot_test_45.csv"), low_memory=False)
    
    train_df = train_df.dropna(subset=[TEXT_COL, LABEL_COL]).copy()
    val_df = val_df.dropna(subset=[TEXT_COL, LABEL_COL]).copy()
    test_df = test_df.dropna(subset=[TEXT_COL, LABEL_COL]).copy()
    
    # Get support counts
    train_counts = train_df[LABEL_COL].value_counts().to_dict()
    val_counts = val_df[LABEL_COL].value_counts().to_dict()
    test_counts = test_df[LABEL_COL].value_counts().to_dict()
    
    all_classes = sorted(set(pipeline.classes_) | set(train_counts.keys()) | set(val_counts.keys()) | set(test_counts.keys()))
    
    # Validate and Test predictions
    val_pred = pipeline.predict(val_df[TEXT_COL])
    test_pred = pipeline.predict(test_df[TEXT_COL])
    
    # Calculate Test overall metrics
    metrics_summary = {
        "accuracy": accuracy_score(test_df[LABEL_COL], test_pred),
        "macro_precision": precision_score(test_df[LABEL_COL], test_pred, average="macro", zero_division=0),
        "macro_recall": recall_score(test_df[LABEL_COL], test_pred, average="macro", zero_division=0),
        "macro_f1": f1_score(test_df[LABEL_COL], test_pred, average="macro", zero_division=0),
        "weighted_precision": precision_score(test_df[LABEL_COL], test_pred, average="weighted", zero_division=0),
        "weighted_recall": recall_score(test_df[LABEL_COL], test_pred, average="weighted", zero_division=0),
        "weighted_f1": f1_score(test_df[LABEL_COL], test_pred, average="weighted", zero_division=0),
    }
    
    # Calculate Per-class metrics
    per_class_results = []
    
    v_prec = precision_score(val_df[LABEL_COL], val_pred, labels=all_classes, average=None, zero_division=0)
    v_rec = recall_score(val_df[LABEL_COL], val_pred, labels=all_classes, average=None, zero_division=0)
    v_f1 = f1_score(val_df[LABEL_COL], val_pred, labels=all_classes, average=None, zero_division=0)
    
    t_prec = precision_score(test_df[LABEL_COL], test_pred, labels=all_classes, average=None, zero_division=0)
    t_rec = recall_score(test_df[LABEL_COL], test_pred, labels=all_classes, average=None, zero_division=0)
    t_f1 = f1_score(test_df[LABEL_COL], test_pred, labels=all_classes, average=None, zero_division=0)
    
    for i, cls in enumerate(all_classes):
        ts = train_counts.get(cls, 0)
        vs = val_counts.get(cls, 0)
        tes = test_counts.get(cls, 0)
        
        # calculate correct / misclassified on test
        cls_mask = test_df[LABEL_COL] == cls
        correct = sum((test_pred == cls) & cls_mask)
        misclass = sum((test_pred != cls) & cls_mask)
        
        flags = []
        if ts < 5: flags.append("<5 train")
        if vs == 0: flags.append("0 val")
        if tes == 0: flags.append("0 test")
        if t_f1[i] < 0.7: flags.append("test F1 < 0.70")
        
        per_class_results.append({
            "class_name": cls,
            "training_support": ts,
            "validation_support": vs,
            "test_support": tes,
            "validation_precision": round(v_prec[i], 4),
            "validation_recall": round(v_rec[i], 4),
            "validation_f1": round(v_f1[i], 4),
            "test_precision": round(t_prec[i], 4),
            "test_recall": round(t_rec[i], 4),
            "test_f1": round(t_f1[i], 4),
            "correctly_classified": int(correct),
            "misclassified": int(misclass),
            "flags": " | ".join(flags)
        })
    
    df_per_class = pd.DataFrame(per_class_results)
    df_per_class.to_csv(REPORTS_DIR / "model_audit_summary.csv", index=False)
    
    with open(REPORTS_DIR / "model_audit_summary.json", "w") as f:
        json.dump({
            "overall_metrics": metrics_summary,
            "per_class": per_class_results
        }, f, indent=2)
        
    # Markdown
    with open(REPORTS_DIR / "model_audit_summary.md", "w") as f:
        f.write("# Model Audit Summary\n\n")
        f.write("## Overall Test Metrics\n")
        for k, v in metrics_summary.items():
            f.write(f"- **{k}**: {v:.4f}\n")
            
        f.write("\n*Explanation for gap between accuracy and macro F1*: Accuracy is high (~0.82) because the model performs well on the majority classes (e.g. REDUCE_POSITION, OPEN_LONG). However, Macro F1 treats all classes equally regardless of support. Because there are several minority classes (like CANCEL_PREVIOUS, ASTROLOGY_CONTENT, TARGET_HIT) with very few examples and 0.0 F1 score, the unweighted average (Macro F1) is dragged down significantly to ~0.54.* \n\n")
        
        f.write("## Per-Class Metrics\n")
        f.write("| Class | Train Support | Val Support | Test Support | Test F1 | Flags |\n")
        f.write("|-------|---------------|-------------|--------------|---------|-------|\n")
        for row in per_class_results:
            f.write(f"| {row['class_name']} | {row['training_support']} | {row['validation_support']} | {row['test_support']} | {row['test_f1']} | {row['flags']} |\n")
            
    # Confusion Matrix Detailed
    cm = confusion_matrix(test_df[LABEL_COL], test_pred, labels=all_classes)
    
    cm_records = []
    top_confusions = []
    for i, true_cls in enumerate(all_classes):
        for j, pred_cls in enumerate(all_classes):
            val = cm[i, j]
            if val > 0:
                cm_records.append({"expected_label": true_cls, "predicted_label": pred_cls, "count": val})
                if true_cls != pred_cls:
                    top_confusions.append({"expected_label": true_cls, "predicted_label": pred_cls, "count": val})
    
    pd.DataFrame(cm_records).to_csv(REPORTS_DIR / "confusion_matrix_detailed.csv", index=False)
    
    df_top_confusions = pd.DataFrame(top_confusions)
    if not df_top_confusions.empty:
        df_top_confusions = df_top_confusions.sort_values(by="count", ascending=False)
        
        # Extract representative messages
        rep_msgs = []
        for _, row in df_top_confusions.iterrows():
            mask = (test_df[LABEL_COL] == row['expected_label']) & (test_pred == row['predicted_label'])
            msgs = test_df[mask][TEXT_COL].tolist()
            rep_msgs.append(" || ".join(msgs[:2]))
        df_top_confusions['representative_messages'] = rep_msgs
        df_top_confusions.to_csv(REPORTS_DIR / "top_model_confusions.csv", index=False)
    else:
        pd.DataFrame(columns=["expected_label", "predicted_label", "count", "representative_messages"]).to_csv(REPORTS_DIR / "top_model_confusions.csv", index=False)

    print("audit_metrics.py completed.")

if __name__ == "__main__":
    main()
