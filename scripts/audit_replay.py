import os
import sqlite3
import pandas as pd
from pathlib import Path
import json

from src.config import resolve_input_path, REPORTS_DIR, STORAGE_DIR, PRODUCTION_MESSAGES_FILE
from src.database import init_db
from src.replay import run_replay, _save_reports
from src.sqlite_repository import (
    SQLitePositionRepository, 
    SQLiteTradeEventRepository,
    SQLiteProcessedMessageRepository,
    SQLiteReviewQueueRepository,
    SQLiteSnapshotRepository
)
from src.holdings_engine import HoldingsEngine
from src.schemas import ParsedTradeEvent
from src.hybrid_parser import HybridParser
from src.validator import validate

def get_table_hashes(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    tables = ["positions", "trade_events", "processed_messages", "manual_review_queue", "position_snapshots"]
    res = {}
    for t in tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        count = cur.fetchone()[0]
        # Just use count as a proxy for hash for now, along with a sum of lengths or something
        cur.execute(f"SELECT * FROM {t}")
        rows = cur.fetchall()
        hash_val = hash(str(rows))
        res[t] = {"count": count, "hash": hash_val, "rows": rows}
    conn.close()
    return res

def test_decimal_precision(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(db_path)
    pos_repo = SQLitePositionRepository(conn)
    event_repo = SQLiteTradeEventRepository(conn)
    proc_repo = SQLiteProcessedMessageRepository(conn)
    rev_repo = SQLiteReviewQueueRepository(conn)
    snap_repo = SQLiteSnapshotRepository(conn)
    
    engine = HoldingsEngine(pos_repo, event_repo, proc_repo, rev_repo, snap_repo)
    
    # Test 100 -> 50 -> 25 -> 12.5 -> 6.25
    pid = "dec_test"
    
    events = [
        ParsedTradeEvent(final_action="OPEN_LONG", symbol="DEC1", quantity_percent="100"),
        ParsedTradeEvent(final_action="REDUCE_POSITION", symbol="DEC1", quantity_percent="50", quantity_basis="CURRENT_HOLDING"),
        ParsedTradeEvent(final_action="REDUCE_POSITION", symbol="DEC1", quantity_percent="50", quantity_basis="CURRENT_HOLDING"),
        ParsedTradeEvent(final_action="REDUCE_POSITION", symbol="DEC1", quantity_percent="50", quantity_basis="CURRENT_HOLDING"),
        ParsedTradeEvent(final_action="REDUCE_POSITION", symbol="DEC1", quantity_percent="50", quantity_basis="CURRENT_HOLDING"),
    ]
    for ev in events: engine.apply_event(ev, pid)
    
    # Test 100 -> 75 -> 56.25
    events2 = [
        ParsedTradeEvent(final_action="OPEN_LONG", symbol="DEC2", quantity_percent="100"),
        ParsedTradeEvent(final_action="REDUCE_POSITION", symbol="DEC2", quantity_percent="25", quantity_basis="CURRENT_HOLDING"),
        ParsedTradeEvent(final_action="REDUCE_POSITION", symbol="DEC2", quantity_percent="25", quantity_basis="CURRENT_HOLDING"),
    ]
    for ev in events2: engine.apply_event(ev, pid)
    
    # Test 80 -> 60
    events3 = [
        ParsedTradeEvent(final_action="OPEN_LONG", symbol="DEC3", quantity_percent="80"),
        ParsedTradeEvent(final_action="REDUCE_POSITION", symbol="DEC3", quantity_percent="25", quantity_basis="CURRENT_HOLDING"),
    ]
    for ev in events3: engine.apply_event(ev, pid)
    
    # Test 50 -> reduced by 33.33%
    events4 = [
        ParsedTradeEvent(final_action="OPEN_LONG", symbol="DEC4", quantity_percent="50"),
        ParsedTradeEvent(final_action="REDUCE_POSITION", symbol="DEC4", quantity_percent="33.33", quantity_basis="CURRENT_HOLDING"),
    ]
    for ev in events4: engine.apply_event(ev, pid)
    
    # Save positions
    for p in pos_repo.list_all_positions():
        pos_repo.save_position(p)
    
    # Query raw DB to check canonical format
    c = conn.cursor()
    c.execute("SELECT symbol, current_allocation_pct, TYPEOF(current_allocation_pct) FROM positions WHERE portfolio_id='dec_test'")
    raw_vals = c.fetchall()
    conn.close()
    
    print("\nDecimal DB Audit:")
    for row in raw_vals:
        print(row)
        
def auto_applied_audit(input_csv, ledger_csv):
    df_in = pd.read_csv(input_csv)
    df_ledger = pd.read_csv(ledger_csv)
    
    applied_ids = df_ledger['source_message_id'].dropna().unique()
    
    parser = HybridParser()
    
    audit_results = []
    
    for _, row in df_in.iterrows():
        msg_id = str(row.get('source_message_id', ''))
        if msg_id not in applied_ids:
            continue
            
        raw_text = str(row.get('raw_text', ''))
        ds_action = str(row.get('action_label', ''))
        
        # fresh parse
        fresh = parser.parse(raw_text, market_group=str(row.get('market_group', '')))
        # validation
        fresh = validate(fresh, confidence=fresh.ml_confidence, already_processed=False)
        
        # Check if safe
        ds_eligible = (
            str(row.get("auto_apply_eligible", "")).lower() in ("true", "1", "yes") and
            not (str(row.get("needs_review", "")).lower() in ("true", "1", "yes")) and
            not (str(row.get("requires_context", "")).lower() in ("true", "1", "yes"))
        )
        
        disagrees = (ds_action and fresh.final_action and ds_action != fresh.final_action)
        
        is_safe = (
            ds_eligible and 
            fresh.auto_apply_eligible and 
            not fresh.needs_review and 
            not fresh.requires_context and 
            not disagrees and 
            fresh.symbol
        )
        
        audit_results.append({
            "source_message_id": msg_id,
            "raw_text": raw_text,
            "dataset_action": ds_action,
            "fresh_final_action": fresh.final_action,
            "dataset_symbol": row.get('symbol', ''),
            "fresh_symbol": fresh.symbol,
            "dataset_auto_apply_eligible": ds_eligible,
            "fresh_validation_passed": fresh.auto_apply_eligible and not fresh.needs_review,
            "dataset_parser_disagreement": disagrees,
            "audit_result": "SAFE" if is_safe else "UNSAFE",
            "audit_reason": "Passes all rules" if is_safe else "Fails one or more rules"
        })
        
    pd.DataFrame(audit_results).to_csv(REPORTS_DIR / "auto_applied_event_audit.csv", index=False)


def main():
    db_path = STORAGE_DIR / "trade_holdings_audit.db"
    
    if db_path.exists():
        print(f"Deleting audit DB: {db_path.resolve()}")
        db_path.unlink()
        
    init_db(db_path)
    print(f"Initialised fresh audit DB: {db_path}")
    
    input_path = resolve_input_path(f"data/{PRODUCTION_MESSAGES_FILE}")
    
    # 1. Run safe-apply FIRST RUN
    print("Running first safe-apply...")
    stats1, pos_repo1, rev_repo1, evt_repo1 = run_replay(input_path, db_path, mode="safe-apply", verbose=False)
    _save_reports(stats1, pos_repo1, rev_repo1, evt_repo1, "safe-apply")
    
    with open(REPORTS_DIR / "first_replay_integrity.json", "w") as f:
        json.dump(stats1.to_dict(), f, indent=2)
        
    # Get DB state
    state1 = get_table_hashes(db_path)
    
    # 2. Run safe-apply SECOND RUN
    print("Running second safe-apply (idempotency)...")
    stats2, pos_repo2, rev_repo2, evt_repo2 = run_replay(input_path, db_path, mode="safe-apply", verbose=False)
    
    state2 = get_table_hashes(db_path)
    
    # Compare DB state
    idemp_diffs = []
    for t in state1.keys():
        if state1[t]['count'] != state2[t]['count'] or state1[t]['hash'] != state2[t]['hash']:
            idemp_diffs.append({"table": t, "count_1": state1[t]['count'], "count_2": state2[t]['count']})
            
    pd.DataFrame(idemp_diffs).to_csv(REPORTS_DIR / "idempotency_differences.csv", index=False)
    with open(REPORTS_DIR / "second_replay_idempotency.json", "w") as f:
        json.dump(stats2.to_dict(), f, indent=2)
        
    # 3. Dry-run
    print("Running dry-run...")
    stats_dry, pos_repo_dry, rev_repo_dry, evt_repo_dry = run_replay(input_path, db_path, mode="dry-run", verbose=False)
    _save_reports(stats_dry, pos_repo_dry, rev_repo_dry, evt_repo_dry, "dry-run")
    
    # Decimal testing
    test_decimal_precision(db_path)
    
    # Audit applied events
    auto_applied_audit(input_path, REPORTS_DIR / "trade_event_ledger.csv")
    
    print("audit_replay.py completed.")

if __name__ == "__main__":
    main()
