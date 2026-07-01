"""
shadow_metrics.py
=================
Generates evaluation metrics for the shadow testing period.
"""

import csv
import json
from pathlib import Path
from src.database import get_connection
from src.config import DATABASE_PATH

def _write_csv(path: Path, data: list):
    if not data:
        return
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)

def generate_shadow_metrics(db_path: Path = DATABASE_PATH, out_dir: str = "reports"):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    
    with get_connection(db_path) as conn:
        # Get total predictions and reviews
        cur = conn.execute("""
            SELECT 
                r.decision,
                p.ml_action, p.final_action, p.symbol, p.quantity_percent,
                v.verified_action, v.verified_symbol, v.verified_quantity_percent, v.was_corrected
            FROM human_reviews r
            JOIN parser_predictions p ON r.prediction_id = p.id
            JOIN verified_labels v ON r.source_message_id = v.source_message_id
        """)
        reviews = [dict(row) for row in cur.fetchall()]
        
        if not reviews:
            print("No reviews found. Metrics skipped.")
            return

        total = len(reviews)
        corrections = sum(1 for r in reviews if r["was_corrected"] == 1)
        approvals = sum(1 for r in reviews if r["decision"] == "APPROVED")
        rejections = sum(1 for r in reviews if r["decision"] == "REJECTED")
        
        # Exact match (complete event accuracy)
        # Note: True "complete event" requires matching direction, stop loss etc., which we simplify here using was_corrected = 0 and decision = APPROVED
        complete_matches = sum(1 for r in reviews if r["was_corrected"] == 0 and r["decision"] == "APPROVED")
        
        action_matches = sum(1 for r in reviews if r["final_action"] == r["verified_action"] and r["final_action"] is not None)
        symbol_matches = sum(1 for r in reviews if r["symbol"] == r["verified_symbol"] and r["symbol"] is not None)
        
        summary = {
            "total_reviews": total,
            "approval_rate": approvals / total,
            "correction_rate": corrections / total,
            "rejection_rate": rejections / total,
            "complete_event_accuracy": complete_matches / total,
            "action_accuracy": action_matches / total,
            "symbol_accuracy": symbol_matches / total
        }
        
        with open(out_path / "shadow_testing_summary.json", 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2)
            
        _write_csv(out_path / "shadow_testing_summary.csv", [summary])
        
        # Action metrics
        cur = conn.execute("""
            SELECT verified_action, COUNT(*) as count, SUM(CASE WHEN was_corrected = 0 THEN 1 ELSE 0 END) as correct
            FROM verified_labels
            GROUP BY verified_action
        """)
        action_metrics = []
        for row in cur.fetchall():
            d = dict(row)
            d["accuracy"] = d["correct"] / d["count"] if d["count"] > 0 else 0
            action_metrics.append(d)
        _write_csv(out_path / "shadow_action_metrics.csv", action_metrics)
        
        # Entity metrics
        cur = conn.execute("""
            SELECT verified_symbol, COUNT(*) as count, SUM(CASE WHEN was_corrected = 0 THEN 1 ELSE 0 END) as correct
            FROM verified_labels
            GROUP BY verified_symbol
        """)
        symbol_metrics = []
        for row in cur.fetchall():
            d = dict(row)
            d["accuracy"] = d["correct"] / d["count"] if d["count"] > 0 else 0
            symbol_metrics.append(d)
        _write_csv(out_path / "shadow_entity_metrics.csv", symbol_metrics)
        
        # Review reasons
        cur = conn.execute("SELECT decision, COUNT(*) as count FROM human_reviews GROUP BY decision")
        _write_csv(out_path / "shadow_review_reason_counts.csv", [dict(row) for row in cur.fetchall()])
        
        # False automatics
        cur = conn.execute("""
            SELECT r.source_message_id, p.final_action as predicted, v.verified_action as actual
            FROM human_reviews r
            JOIN parser_predictions p ON r.prediction_id = p.id
            JOIN verified_labels v ON r.source_message_id = v.source_message_id
            WHERE p.validation_status = 'VALID' AND r.decision != 'APPROVED'
        """)
        _write_csv(out_path / "shadow_false_automatic_events.csv", [dict(row) for row in cur.fetchall()])
        
        print("Generated shadow testing metrics in reports/")

if __name__ == "__main__":
    generate_shadow_metrics()
