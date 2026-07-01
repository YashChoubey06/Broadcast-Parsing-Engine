"""
ingest_shadow_csv.py
====================
CLI tool to ingest multiple messages from a CSV into the shadow testing environment.
"""

import argparse
import csv
import sys
from pathlib import Path
from src.shadow_service import ingest_message
from src.config import DATABASE_PATH

def main():
    parser = argparse.ArgumentParser(description="Ingest multiple messages from a CSV for shadow testing.")
    parser.add_argument("--input", required=True, help="Path to input CSV file")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"Error: File not found -> {input_path}")
        sys.exit(1)

    ingested_count = 0
    duplicate_count = 0

    with open(input_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            msg_id = row.get("source_message_id", row.get("id"))
            text = row.get("text", row.get("raw_text"))
            segment = row.get("segment", row.get("market", "Unknown"))
            
            if not msg_id or not text:
                print(f"Skipping row missing id or text: {row}")
                continue
                
            res = ingest_message(msg_id, text, segment, db_path=DATABASE_PATH)
            if res["status"] == "DUPLICATE":
                duplicate_count += 1
            else:
                ingested_count += 1
                
    print(f"CSV Ingestion complete.")
    print(f"Successfully ingested: {ingested_count}")
    print(f"Duplicates ignored: {duplicate_count}")

if __name__ == "__main__":
    main()
