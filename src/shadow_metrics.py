"""
shadow_metrics.py
=================
Generates evaluation metrics for the shadow testing period.
"""

import csv
import json
from pathlib import Path
from contextlib import closing
from src.database import get_connection
from src.config import DATABASE_PATH

def _write_csv(path: Path, data: list):
    if not data:
        return
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)

def _safe_rate(num: int, den: int) -> float:
    return num / den if den else 0.0


def generate_shadow_metrics(db_path: Path = DATABASE_PATH, out_dir: str = "reports"):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    
    with closing(get_connection(db_path)) as conn:
        # Get total predictions and reviews
        cur = conn.execute("""
            SELECT 
                r.decision,
                p.ml_action, p.final_action, p.symbol, p.quantity_percent, p.prediction_json,
                v.verified_action, v.verified_symbol, v.verified_direction,
                v.verified_quantity_percent, v.was_corrected
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
        corrected_approvals = sum(1 for r in reviews if r["decision"] == "APPROVED_WITH_CORRECTION")
        rejections = sum(1 for r in reviews if r["decision"] == "REJECTED")
        manual_reviews = sum(1 for r in reviews if r["decision"] in {"APPROVED_WITH_CORRECTION", "REJECTED", "NON_TRADE", "NEEDS_CONTEXT"})
        
        # Exact match (complete event accuracy)
        # Note: True "complete event" requires matching direction, stop loss etc., which we simplify here using was_corrected = 0 and decision = APPROVED
        complete_matches = sum(1 for r in reviews if r["was_corrected"] == 0 and r["decision"] == "APPROVED")
        
        action_matches = sum(1 for r in reviews if r["final_action"] == r["verified_action"] and r["final_action"] is not None)
        symbol_matches = sum(1 for r in reviews if r["symbol"] == r["verified_symbol"] and r["symbol"] is not None)
        percentage_matches = sum(1 for r in reviews if str(r["quantity_percent"]) == str(r["verified_quantity_percent"]) and r["quantity_percent"] is not None)
        direction_matches = 0
        for r in reviews:
            try:
                predicted_direction = json.loads(r["prediction_json"]).get("direction")
            except Exception:
                predicted_direction = None
            if predicted_direction == r["verified_direction"] and predicted_direction is not None:
                direction_matches += 1

        verified_positions = [dict(row) for row in conn.execute(
            "SELECT symbol, direction, current_allocation_pct FROM positions WHERE portfolio_id='verified' AND status='OPEN' ORDER BY symbol, direction"
        ).fetchall()]
        shadow_positions = [dict(row) for row in conn.execute(
            "SELECT symbol, direction, current_allocation_pct FROM positions WHERE portfolio_id='shadow' AND status='OPEN' ORDER BY symbol, direction"
        ).fetchall()]
        shadow_by_key = {(p["symbol"], p["direction"]): p for p in shadow_positions}
        verified_by_key = {(p["symbol"], p["direction"]): p for p in verified_positions}
        position_diffs = []
        for key in sorted(set(shadow_by_key) | set(verified_by_key)):
            v = verified_by_key.get(key, {})
            s = shadow_by_key.get(key, {})
            position_diffs.append({
                "symbol": key[0],
                "direction": key[1],
                "verified_allocation_pct": v.get("current_allocation_pct"),
                "shadow_allocation_pct": s.get("current_allocation_pct"),
                "matches": v.get("current_allocation_pct") == s.get("current_allocation_pct"),
            })
        
        summary = {
            "total_reviews": total,
            "approval_rate": _safe_rate(approvals + corrected_approvals, total),
            "correction_rate": _safe_rate(corrections, total),
            "rejection_rate": _safe_rate(rejections, total),
            "manual_review_rate": _safe_rate(manual_reviews, total),
            "complete_event_accuracy": _safe_rate(complete_matches, total),
            "action_accuracy": _safe_rate(action_matches, total),
            "symbol_accuracy": _safe_rate(symbol_matches, total),
            "percentage_accuracy": _safe_rate(percentage_matches, total),
            "direction_accuracy": _safe_rate(direction_matches, total),
            "verified_shadow_position_differences": sum(1 for row in position_diffs if not row["matches"]),
            "metric_notes": {
                "complete_event_accuracy": "Approximation: approved-as-parsed and not corrected, not a field-perfect equality across every event attribute.",
                "manual_review_rate": "Approximation over persisted human review decisions in the local shadow-review database."
            }
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
        _write_csv(out_path / "shadow_position_differences.csv", position_diffs)
        
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
    import argparse

    parser = argparse.ArgumentParser(description="Generate shadow-review metrics.")
    parser.add_argument("--db", default=str(DATABASE_PATH), help="SQLite database path")
    parser.add_argument("--out-dir", default="reports", help="Report output directory")
    args = parser.parse_args()
    generate_shadow_metrics(Path(args.db), args.out_dir)
