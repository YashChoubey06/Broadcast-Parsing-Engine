"""
export_verified_training_data.py
================================
Exports human-verified labels into a gold-standard CSV.
"""

import csv
import argparse
from pathlib import Path

from src.database import get_connection
from src.config import DATABASE_PATH

def export_labels(output_file: Path, db_path: Path = DATABASE_PATH):
    query = """
        SELECT 
            v.source_message_id,
            v.raw_text,
            i.normalized_text,
            v.verified_action as `verified action label`,
            v.verified_symbol as `verified symbol`,
            v.verified_direction as `direction`,
            v.verified_quantity_percent as `percentage`,
            v.verified_quantity_basis as `quantity basis`,
            v.verified_entry_prices_json as `entry prices`,
            v.verified_stop_loss as `stop loss`,
            v.verified_targets_json as `targets`,
            r.decision as `human decision`,
            CASE WHEN v.was_corrected = 0 THEN 'YES' ELSE 'NO' END as `whether parser prediction was correct`,
            p.parser_version as `parser version`,
            p.model_version as `model version`,
            r.reviewed_at as `review timestamp`
        FROM verified_labels v
        JOIN incoming_messages i ON v.source_message_id = i.source_message_id
        JOIN parser_predictions p ON v.source_message_id = p.source_message_id
        JOIN human_reviews r ON v.source_message_id = r.source_message_id
        GROUP BY v.source_message_id
        ORDER BY r.reviewed_at ASC
    """
    
    with get_connection(db_path) as conn:
        cur = conn.execute(query)
        rows = cur.fetchall()
        
        if not rows:
            print("No verified labels found. Nothing to export.")
            return
            
        columns = rows[0].keys()
        
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
                
        print(f"Successfully exported {len(rows)} verified labels to {output_file}")

def main():
    parser = argparse.ArgumentParser(description="Export verified shadow labels.")
    parser.add_argument("--output", default="data/verified_shadow_labels.csv", help="Output path")
    args = parser.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    export_labels(out_path)

if __name__ == "__main__":
    main()
