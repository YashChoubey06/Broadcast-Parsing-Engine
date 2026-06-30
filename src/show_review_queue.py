"""
show_review_queue.py
====================
CLI: Show pending manual review items.

    python -m src.show_review_queue
    python -m src.show_review_queue --all
"""

from __future__ import annotations

import argparse

from src.config import DATABASE_PATH
from src.database import get_connection, init_db
from src.sqlite_repository import SQLiteReviewQueueRepository


def show_review_queue(show_all: bool = False) -> None:
    init_db(DATABASE_PATH)
    conn = get_connection(DATABASE_PATH)
    repo = SQLiteReviewQueueRepository(conn)
    items = repo.list_all() if show_all else repo.list_pending()
    conn.close()

    label = "ALL" if show_all else "PENDING"
    if not items:
        print(f"No {label.lower()} review items.")
        return

    print(f"\n{'='*90}")
    print(f"  MANUAL REVIEW QUEUE — {label} items")
    print(f"{'='*90}")

    for item in items:
        print(f"\n  ID: {item.id}  |  Status: {item.review_status}")
        print(f"  Message: {item.source_message_id}")
        print(f"  Text:    {item.raw_text[:80]}")
        print(f"  Reason:  {item.review_reason}")
        if item.resolution_notes:
            print(f"  Notes:   {item.resolution_notes}")
        print(f"  {'─'*80}")

    print(f"\n  Total {label.lower()} items: {len(items)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Show the manual review queue.")
    parser.add_argument("--all", action="store_true", help="Show all items (including resolved).")
    args = parser.parse_args()
    show_review_queue(args.all)


if __name__ == "__main__":
    main()
