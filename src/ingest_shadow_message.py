"""
ingest_shadow_message.py
========================
CLI tool to ingest a single message into the shadow testing environment.
"""

import argparse
from src.shadow_service import ingest_message
from src.config import DATABASE_PATH

def main():
    parser = argparse.ArgumentParser(description="Ingest a single message for shadow testing and review.")
    parser.add_argument("--message-id", required=True, help="Unique source message ID")
    parser.add_argument("--text", required=True, help="Raw message text")
    parser.add_argument("--segment", default="Unknown", help="Market segment or group")
    args = parser.parse_args()

    result = ingest_message(
        source_message_id=args.message_id,
        raw_text=args.text,
        segment_name=args.segment,
        db_path=DATABASE_PATH
    )
    
    if result["status"] == "DUPLICATE":
        print(f"Message {args.message_id} is already in the system. Ignored.")
    else:
        event = result["event"]
        print(f"Ingested: {args.message_id}")
        print(f"Action: {event.final_action}")
        print(f"Symbol: {event.symbol}")
        print(f"Confidence: {event.ml_confidence}")
        print(f"Status: {result['status']}")
        
if __name__ == "__main__":
    main()
