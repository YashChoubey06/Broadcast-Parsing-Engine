"""
show_history.py
===============
CLI: Show the trade-event history for a specific symbol.

    python -m src.show_history --symbol NIFTY
    python -m src.show_history --symbol CRUDE
"""

from __future__ import annotations

import argparse

from src.config import DATABASE_PATH
from src.database import get_connection, init_db
from src.sqlite_repository import SQLiteTradeEventRepository


def show_history(symbol: str) -> None:
    init_db(DATABASE_PATH)
    conn = get_connection(DATABASE_PATH)
    repo = SQLiteTradeEventRepository(conn)
    events = repo.list_events_for_symbol(symbol.upper())
    conn.close()

    if not events:
        print(f"No events found for symbol: {symbol.upper()}")
        return

    print(f"\n{'='*90}")
    print(f"  TRADE EVENT HISTORY — {symbol.upper()}")
    print(f"{'='*90}")
    print(
        f"  {'ID':<6} {'Order':<7} {'Action':<25} {'Qty%':<8} "
        f"{'Source':<22} {'Status':<15}"
    )
    print(f"  {'-'*85}")

    for e in events:
        print(
            f"  {e.get('id',''):<6} "
            f"{e.get('processing_order',''):<7} "
            f"{e.get('final_action',''):<25} "
            f"{e.get('quantity_percent') or '-':<8} "
            f"{e.get('resolution_source',''):<22} "
            f"{e.get('processing_status',''):<15}"
        )

    print(f"{'='*90}")
    print(f"  Total events: {len(events)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Show trade event history for a symbol.")
    parser.add_argument("--symbol", required=True, help="Instrument symbol (e.g. NIFTY).")
    args = parser.parse_args()
    show_history(args.symbol)


if __name__ == "__main__":
    main()
