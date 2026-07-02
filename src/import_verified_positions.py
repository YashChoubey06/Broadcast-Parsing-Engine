"""
import_verified_positions.py
============================
Safely import initial positions for the verified portfolio.
"""

import argparse
import csv
import sys
import json
from pathlib import Path
from datetime import datetime, timezone
from contextlib import closing

from src.database import get_connection, transaction
from src.config import DATABASE_PATH
from src.position_identity import IdentityMatchStatus, canonicalize_position
from src.sqlite_repository import SQLitePositionRepository
from src.schemas import PositionState

def _parse_row(row: dict) -> PositionState:
    return PositionState(
        portfolio_id="verified", # FORCE VERIFIED
        market_group=row.get("market_group"),
        symbol=row["symbol"],
        contract_month=row.get("contract_month"),
        option_type=row.get("option_type"),
        strike_price=row.get("strike_price"),
        direction=row["direction"],
        current_allocation_pct=row.get("current_allocation_pct", "0"),
        average_entry_price=row.get("average_entry_price"),
        stop_loss=row.get("stop_loss"),
        targets=[t.strip() for t in row.get("targets", "").split(",") if t.strip()],
        status=row.get("status", "OPEN"),
        opened_at=row.get("as_of_timestamp", datetime.now(timezone.utc).isoformat()),
        updated_at=row.get("as_of_timestamp", datetime.now(timezone.utc).isoformat())
    )

def import_positions(input_path: Path, db_path: Path = DATABASE_PATH, dry_run: bool = False, apply: bool = False, confirm: bool = False) -> int:
    if apply and not confirm:
        print("Error: --apply requires --confirm to be explicitly set.")
        return 1

    if not dry_run and not apply:
        print("Error: Must specify either --dry-run or --apply --confirm")
        return 1

    if not input_path.is_file():
        print(f"Error: File not found -> {input_path}")
        return 1

    with closing(get_connection(db_path)) as conn:
        positions_to_insert = []
        with open(input_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                pos = _parse_row(row)
                pos = canonicalize_position(pos)

                repo = SQLitePositionRepository(conn)
                existing = repo.resolve_position(
                    portfolio_id="verified",
                    symbol=pos.symbol,
                    direction=pos.direction,
                    market_group=pos.market_group,
                    contract_month=pos.contract_month,
                    option_type=pos.option_type,
                    strike_price=pos.strike_price,
                )
                if existing.status in {
                    IdentityMatchStatus.EXACT_MATCH,
                    IdentityMatchStatus.UNIQUE_FALLBACK_MATCH,
                    IdentityMatchStatus.AMBIGUOUS_MATCH,
                }:
                    print(f"Row {i+1}: OVERWRITE PREVENTED. Existing open {pos.direction} position found for {pos.symbol}.")
                    continue

                positions_to_insert.append(pos)

        if dry_run:
            print("--- DRY RUN ---")
            print(f"Would import {len(positions_to_insert)} new verified positions:")
            for p in positions_to_insert:
                print(f"  + {p.direction} {p.symbol} @ {p.current_allocation_pct}%")
            print("No database changes made.")
            return 0

        if apply and confirm:
            now = datetime.now(timezone.utc).isoformat()
            with transaction(db_path) as tx_conn:
                for p in positions_to_insert:
                    cur = tx_conn.execute("""
                        INSERT INTO positions (
                            portfolio_id, market_group, symbol, contract_month, option_type, strike_price,
                            direction, current_allocation_pct, average_entry_price, stop_loss, targets_json,
                            status, opened_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        p.portfolio_id, p.market_group, p.symbol, p.contract_month, p.option_type,
                        str(p.strike_price) if p.strike_price else "",
                        p.direction, p.current_allocation_pct, p.average_entry_price, p.stop_loss, json.dumps(p.targets),
                        p.status, p.opened_at, p.updated_at
                    ))

                    p.position_id = cur.lastrowid
                    event_id = f"import_{int(datetime.now().timestamp())}_{p.symbol}_{p.direction}"
                    tx_conn.execute("""
                        INSERT INTO incoming_messages (
                            source_message_id, raw_text, normalized_text, segment_name,
                            created_at, received_at, processing_status, text_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        event_id, "CSV Import", "CSV Import", p.market_group,
                        now, now, "INITIAL_SETUP", None,
                    ))
                    fake_event_json = json.dumps({
                        "source_message_id": event_id,
                        "raw_text": "CSV Import",
                        "record_type": "INITIAL_SETUP",
                        "final_action": "INITIAL_SETUP",
                        "symbol": p.symbol,
                        "direction": p.direction,
                        "quantity_percent": str(p.current_allocation_pct)
                    })
                    pos_json = json.dumps({
                        "id": p.position_id,
                        "portfolio_id": p.portfolio_id,
                        "symbol": p.symbol,
                        "direction": p.direction,
                        "current_allocation_pct": str(p.current_allocation_pct),
                        "status": p.status
                    })

                    tx_conn.execute("""
                        INSERT INTO verified_events (
                            source_message_id, approved_event_json, position_after_json, applied_at
                        ) VALUES (?, ?, ?, ?)
                    """, (event_id, fake_event_json, pos_json, now))

            print(f"Successfully applied {len(positions_to_insert)} verified positions.")
            return 0

    return 0


def main():
    parser = argparse.ArgumentParser(description="Import verified positions safely.")
    parser.add_argument("--input", required=True, help="Path to initial positions CSV")
    parser.add_argument("--dry-run", action="store_true", help="Show proposed changes without writing")
    parser.add_argument("--apply", action="store_true", help="Actually apply the changes")
    parser.add_argument("--confirm", action="store_true", help="Confirmation flag required for --apply")
    parser.add_argument("--db", default=str(DATABASE_PATH), help="SQLite database path")
    args = parser.parse_args()
    sys.exit(import_positions(Path(args.input), Path(args.db), args.dry_run, args.apply, args.confirm))

if __name__ == "__main__":
    main()
