"""
parse_message.py
================
CLI: Parse a single raw trade message and print the structured JSON result.

Usage
-----
    python -m src.parse_message --text "50% PROFIT BOOK IN NIFTY @25942"

With holdings context (resolves SELL ambiguity):
    python -m src.parse_message \\
        --text "SELL 50% NIFTY" \\
        --portfolio default \\
        --use-db-context
"""

from __future__ import annotations

import argparse
import json

from src.config import DATABASE_PATH, DEFAULT_PORTFOLIO_ID
from src.hybrid_parser import HybridParser


def parse_and_print(
    text: str,
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
    use_db_context: bool = False,
    source_message_id: str | None = None,
) -> None:
    existing_long = False
    existing_short = False
    has_holdings_context = False

    if use_db_context:
        try:
            import sqlite3
            from src.database import get_connection
            from src.sqlite_repository import SQLitePositionRepository

            conn = get_connection(DATABASE_PATH)
            repo = SQLitePositionRepository(conn)

            # We'll check after parsing (to get the symbol)
            # Do a pre-parse just for the symbol
            pre_parser = HybridParser()
            pre_event = pre_parser.parse(text)
            symbol = pre_event.symbol or ""

            if symbol:
                long_pos = repo.get_position(portfolio_id, symbol, direction="LONG")
                short_pos = repo.get_position(portfolio_id, symbol, direction="SHORT")
                existing_long = bool(long_pos and long_pos.status == "OPEN")
                existing_short = bool(short_pos and short_pos.status == "OPEN")
                has_holdings_context = True
            conn.close()
        except Exception as e:
            print(f"Warning: Could not load holdings context: {e}")

    parser = HybridParser()
    event = parser.parse(
        raw_text=text,
        source_message_id=source_message_id,
        existing_long=existing_long,
        existing_short=existing_short,
        has_holdings_context=has_holdings_context,
    )
    print(event.to_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse a single trade message and print structured JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--text", required=True, help="Raw trade message text.")
    parser.add_argument(
        "--portfolio", default=DEFAULT_PORTFOLIO_ID,
        help="Portfolio ID for holdings context lookup."
    )
    parser.add_argument(
        "--use-db-context", action="store_true",
        help="Load current holdings from the database to resolve SELL ambiguity."
    )
    parser.add_argument(
        "--message-id", default=None,
        help="Optional source message ID to assign."
    )
    args = parser.parse_args()
    parse_and_print(
        text=args.text,
        portfolio_id=args.portfolio,
        use_db_context=args.use_db_context,
        source_message_id=args.message_id,
    )


if __name__ == "__main__":
    main()
