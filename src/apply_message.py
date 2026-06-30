"""
apply_message.py
================
CLI: Parse a single message and apply it to the holdings database.

Usage
-----
    python -m src.apply_message \\
        --message-id test-001 \\
        --text "BUY 50% NVDA @145"

The message is parsed, validated, and – if eligible – applied to holdings.
Ineligible messages go to the manual-review queue.
"""

from __future__ import annotations

import argparse
import json

from src.config import DATABASE_PATH, DEFAULT_PORTFOLIO_ID
from src.database import get_connection, init_db
from src.holdings_engine import HoldingsEngine
from src.hybrid_parser import HybridParser
from src.sqlite_repository import (
    SQLitePositionRepository,
    SQLiteProcessedMessageRepository,
    SQLiteReviewQueueRepository,
    SQLiteSnapshotRepository,
    SQLiteTradeEventRepository,
)
from src.validator import validate


def apply_message(
    text: str,
    message_id: str,
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
) -> None:
    init_db(DATABASE_PATH)
    conn = get_connection(DATABASE_PATH)

    try:
        pos_repo = SQLitePositionRepository(conn)
        event_repo = SQLiteTradeEventRepository(conn)
        processed_repo = SQLiteProcessedMessageRepository(conn)
        review_repo = SQLiteReviewQueueRepository(conn)
        snapshot_repo = SQLiteSnapshotRepository(conn)

        # Check holdings context for SELL resolution
        symbol_hint = None
        existing_long = False
        existing_short = False

        pre_parser = HybridParser()
        pre_event = pre_parser.parse(text)
        symbol_hint = pre_event.symbol

        if symbol_hint:
            long_pos = pos_repo.get_position(portfolio_id, symbol_hint, direction="LONG")
            short_pos = pos_repo.get_position(portfolio_id, symbol_hint, direction="SHORT")
            existing_long = bool(long_pos and long_pos.status == "OPEN")
            existing_short = bool(short_pos and short_pos.status == "OPEN")

        already_processed = processed_repo.is_processed(message_id)

        # Parse with holdings context
        parser = HybridParser()
        event = parser.parse(
            raw_text=text,
            source_message_id=message_id,
            existing_long=existing_long,
            existing_short=existing_short,
            has_holdings_context=True,
        )

        # Validate
        event = validate(event, confidence=event.ml_confidence, already_processed=already_processed)

        print(f"Parsed event:")
        print(f"  final_action: {event.final_action}")
        print(f"  symbol: {event.symbol}")
        print(f"  quantity_percent: {event.quantity_percent}")
        print(f"  auto_apply_eligible: {event.auto_apply_eligible}")
        print(f"  needs_review: {event.needs_review}")
        if event.validation_errors:
            print(f"  errors: {event.validation_errors}")
        if event.validation_warnings:
            print(f"  warnings: {event.validation_warnings}")

        engine = HoldingsEngine(
            position_repo=pos_repo,
            event_repo=event_repo,
            processed_repo=processed_repo,
            review_repo=review_repo,
            snapshot_repo=snapshot_repo,
            portfolio_id=portfolio_id,
        )

        if event.auto_apply_eligible:
            result = engine.apply_event(event, processing_order=0)
        else:
            result = engine.apply_event(event, processing_order=0)

        conn.commit()

        print(f"\nResult:")
        print(f"  status: {result.status}")
        print(f"  message: {result.message}")
        if result.position_after:
            pa = result.position_after
            print(f"  position_after: {pa.symbol} {pa.direction} {pa.current_allocation_pct}% [{pa.status}]")

    except Exception as e:
        conn.rollback()
        print(f"Error: {e}")
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse and apply a single trade message to the holdings database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--message-id", required=True, help="Unique message identifier.")
    parser.add_argument("--text", required=True, help="Raw trade message text.")
    parser.add_argument("--portfolio", default=DEFAULT_PORTFOLIO_ID, help="Portfolio ID.")
    args = parser.parse_args()
    apply_message(args.text, args.message_id, args.portfolio)


if __name__ == "__main__":
    main()
