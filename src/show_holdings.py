"""
show_holdings.py
================
CLI: Display current open positions in a human-readable table.

    python -m src.show_holdings
    python -m src.show_holdings --portfolio default --all
"""

from __future__ import annotations

import argparse

from src.config import DATABASE_PATH, DEFAULT_PORTFOLIO_ID
from src.database import get_connection, init_db
from src.sqlite_repository import SQLitePositionRepository


def show_holdings(portfolio_id: str = DEFAULT_PORTFOLIO_ID, show_all: bool = False) -> None:
    init_db(DATABASE_PATH)
    conn = get_connection(DATABASE_PATH)
    repo = SQLitePositionRepository(conn)

    if show_all:
        positions = repo.list_all_positions(portfolio_id)
        title = "ALL POSITIONS"
    else:
        positions = repo.list_open_positions(portfolio_id)
        title = "OPEN POSITIONS"

    conn.close()

    if not positions:
        print(f"No {title.lower()} found for portfolio '{portfolio_id}'.")
        return

    print(f"\n{'='*80}")
    print(f"  {title} — Portfolio: {portfolio_id}")
    print(f"{'='*80}")
    print(
        f"  {'#':<4} {'Symbol':<22} {'Dir':<6} {'Alloc%':<10} "
        f"{'Avg Entry':<14} {'Stop Loss':<12} {'Status':<8}"
    )
    print(f"  {'-'*72}")

    for i, p in enumerate(positions, 1):
        entry = str(p.average_entry_price) if p.average_entry_price else "-"
        sl = str(p.stop_loss) if p.stop_loss else "-"
        print(
            f"  {i:<4} {p.symbol:<22} {p.direction or '?':<6} "
            f"{str(p.current_allocation_pct):<10} {entry:<14} {sl:<12} {p.status:<8}"
        )
    print(f"{'='*80}")
    print(f"  Total: {len(positions)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Show current holdings.")
    parser.add_argument("--portfolio", default=DEFAULT_PORTFOLIO_ID)
    parser.add_argument("--all", action="store_true", help="Show all positions including closed.")
    args = parser.parse_args()
    show_holdings(args.portfolio, args.all)


if __name__ == "__main__":
    main()
