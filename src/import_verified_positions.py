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

from src.database import get_connection, transaction
from src.config import DATABASE_PATH
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

def main():
    parser = argparse.ArgumentParser(description="Import verified positions safely.")
    parser.add_argument("--input", required=True, help="Path to initial positions CSV")
    parser.add_argument("--dry-run", action="store_true", help="Show proposed changes without writing")
    parser.add_argument("--apply", action="store_true", help="Actually apply the changes")
    parser.add_argument("--confirm", action="store_true", help="Confirmation flag required for --apply")
    args = parser.parse_args()

    if args.apply and not args.confirm:
        print("Error: --apply requires --confirm to be explicitly set.")
        sys.exit(1)
        
    if not args.dry_run and not args.apply:
        print("Error: Must specify either --dry-run or --apply --confirm")
        sys.exit(1)

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"Error: File not found -> {input_path}")
        sys.exit(1)

    with get_connection(DATABASE_PATH) as conn:
        positions_to_insert = []
        with open(input_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                pos = _parse_row(row)
                
                # Check for existing
                cur = conn.execute("""
                    SELECT id FROM positions 
                    WHERE portfolio_id = 'verified' AND symbol = ? AND direction = ? AND status = 'OPEN'
                """, (pos.symbol, pos.direction))
                
                if cur.fetchone():
                    print(f"Row {i+1}: OVERWRITE PREVENTED. Existing open {pos.direction} position found for {pos.symbol}.")
                    continue
                
                positions_to_insert.append(pos)
                
        if args.dry_run:
            print("--- DRY RUN ---")
            print(f"Would import {len(positions_to_insert)} new verified positions:")
            for p in positions_to_insert:
                print(f"  + {p.direction} {p.symbol} @ {p.current_allocation_pct}%")
            print("No database changes made.")
            return

        if args.apply and args.confirm:
            now = datetime.now(timezone.utc).isoformat()
            # We must create immutable initial-position events too.
            with transaction(DATABASE_PATH) as tx_conn:
                for p in positions_to_insert:
                    cur = tx_conn.execute("""
                        INSERT INTO positions (
                            portfolio_id, market_group, symbol, contract_month, option_type, strike_price,
                            direction, current_allocation_pct, average_entry_price, stop_loss, targets_json,
                            status, opened_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        p.portfolio_id, p.market_group, p.symbol, p.contract_month, p.option_type, p.strike_price,
                        p.direction, p.current_allocation_pct, p.average_entry_price, p.stop_loss, json.dumps(p.targets),
                        p.status, p.opened_at, p.updated_at
                    ))
                    
                    pos_after = p
                    pos_after.id = cur.lastrowid
                    
                    # Also insert into verified_events as an INITIAL_SETUP record
                    event_id = f"import_{int(datetime.now().timestamp())}_{p.symbol}_{p.direction}"
                    
                    fake_event_json = json.dumps({
                        "source_message_id": event_id,
                        "raw_text": "CSV Import",
                        "record_type": "INITIAL_SETUP",
                        "final_action": "INITIAL_SETUP",
                        "symbol": p.symbol,
                        "direction": p.direction,
                        "quantity_percent": p.current_allocation_pct
                    })
                    
                    pos_json = json.dumps({
                        "id": p.id,
                        "portfolio_id": p.portfolio_id,
                        "symbol": p.symbol,
                        "direction": p.direction,
                        "current_allocation_pct": p.current_allocation_pct,
                        "status": p.status
                    })
                    
                    tx_conn.execute("""
                        INSERT INTO verified_events (
                            source_message_id, approved_event_json, position_after_json, applied_at
                        ) VALUES (?, ?, ?, ?)
                    """, (event_id, fake_event_json, pos_json, now))

            print(f"Successfully applied {len(positions_to_insert)} verified positions.")

if __name__ == "__main__":
    main()
