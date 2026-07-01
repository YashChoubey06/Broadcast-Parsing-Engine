import pytest
import sqlite3
import json
import csv
from pathlib import Path
from decimal import Decimal

from src.database import get_connection, init_db, drop_all_tables
from src.shadow_service import (
    ingest_message,
    approve_as_parsed,
    edit_and_approve,
    reject,
    mark_non_trade,
    mark_needs_context,
    get_pending_reviews
)
from src.export_verified_training_data import export_labels
from src.import_verified_positions import import_positions
from scripts.migrate_shadow_tables import apply_migration

TEST_DB_PATH = Path("storage/test_shadow_workflow.db")

@pytest.fixture(scope="function", autouse=True)
def setup_test_db():
    # Clean setup
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
        
    init_db(TEST_DB_PATH)
    # Patch the DATABASE_PATH inside migrate_shadow_tables
    import scripts.migrate_shadow_tables
    scripts.migrate_shadow_tables.DATABASE_PATH = TEST_DB_PATH
    apply_migration()
    
    yield TEST_DB_PATH
    
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()

from contextlib import closing

def get_pos(db_path, portfolio_id, symbol, direction="LONG"):
    with closing(get_connection(db_path)) as conn:
        return conn.execute(
            "SELECT * FROM positions WHERE portfolio_id=? AND symbol=? AND direction=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
            (portfolio_id, symbol, direction)
        ).fetchone()

def test_migration_is_idempotent():
    # Already applied in setup
    apply_migration() # Second time
    # Check it didn't fail and schema_migrations has 1 entry
    with closing(get_connection(TEST_DB_PATH)) as conn:
        cur = conn.execute("SELECT COUNT(*) as c FROM schema_migrations WHERE migration_name='001_shadow_testing_tables'")
        assert cur.fetchone()["c"] == 1

def test_historical_tables_untouched():
    # Ensure processed_messages and trade_events are empty but exist
    with closing(get_connection(TEST_DB_PATH)) as conn:
        assert conn.execute("SELECT COUNT(*) as c FROM processed_messages").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) as c FROM trade_events").fetchone()["c"] == 0

def test_shadow_prediction_does_not_change_verified():
    res = ingest_message("msg1", "Buy NIFTY 100% at CMP", "Indices", TEST_DB_PATH)
    assert res["status"] == "PENDING_REVIEW"
    
    with closing(get_connection(TEST_DB_PATH)) as conn:
        for row in conn.execute("SELECT * FROM positions").fetchall():
            print("DB POS:", dict(row))
    
    s_pos = get_pos(TEST_DB_PATH, "shadow", "NIFTY")
    assert s_pos is not None
    assert s_pos["current_allocation_pct"] == "100"
    
    v_pos = get_pos(TEST_DB_PATH, "verified", "NIFTY")
    assert v_pos is None

def test_approving_prediction_updates_verified():
    ingest_message("msg1", "Buy NIFTY 100% at CMP", "Indices", TEST_DB_PATH)
    res = approve_as_parsed("msg1", "TestReviewer", TEST_DB_PATH)
    assert res["status"] == "APPROVED"
    
    v_pos = get_pos(TEST_DB_PATH, "verified", "NIFTY")
    assert v_pos is not None
    assert v_pos["current_allocation_pct"] == "100"

def test_edit_and_approve_applies_correction():
    res = ingest_message("msg2", "Buy BANKNIFTY 100% at CMP", "Indices", TEST_DB_PATH)
    event = res["event"]
    event.quantity_percent = Decimal("50")
    
    res2 = edit_and_approve("msg2", "Tester", event, {"qty": "50"}, "Fixed qty", TEST_DB_PATH)
    assert res2["status"] == "APPROVED"
    
    v_pos = get_pos(TEST_DB_PATH, "verified", "BANKNIFTY")
    assert v_pos is not None
    assert v_pos["current_allocation_pct"] == "50"
    
    # Original prediction immutable
    with closing(get_connection(TEST_DB_PATH)) as conn:
        cur = conn.execute("SELECT quantity_percent FROM parser_predictions WHERE source_message_id='msg2'")
        assert cur.fetchone()["quantity_percent"] == "100"
        
        # verified_labels has was_corrected = 1
        cur = conn.execute("SELECT was_corrected FROM verified_labels WHERE source_message_id='msg2'")
        assert cur.fetchone()["was_corrected"] == 1

def test_rejection_does_not_change_verified():
    ingest_message("msg3", "Buy BANKNIFTY 100% at CMP", "Indices", TEST_DB_PATH)
    reject("msg3", "Tester", "Bad trade", TEST_DB_PATH)
    
    v_pos = get_pos(TEST_DB_PATH, "verified", "BANKNIFTY")
    assert v_pos is None
    
    s_pos = get_pos(TEST_DB_PATH, "shadow", "BANKNIFTY")
    assert s_pos is not None # Shadow history preserved

def test_duplicate_ingestion_ignored():
    ingest_message("msg4", "Buy GOLD 100% at CMP", "Commodities", TEST_DB_PATH)
    res = ingest_message("msg4", "Buy GOLD 100% at CMP", "Commodities", TEST_DB_PATH)
    assert res["status"] == "DUPLICATE"
    
    with closing(get_connection(TEST_DB_PATH)) as conn:
        assert conn.execute("SELECT COUNT(*) as c FROM incoming_messages").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) as c FROM parser_predictions").fetchone()["c"] == 1

def test_duplicate_review_decision_prevented():
    ingest_message("msg5", "Buy SILVER 100% at CMP", "Commodities", TEST_DB_PATH)
    res1 = approve_as_parsed("msg5", "R1", TEST_DB_PATH)
    assert res1["status"] == "APPROVED"
    
    res2 = approve_as_parsed("msg5", "R2", TEST_DB_PATH)
    assert res2["status"] == "ALREADY_REVIEWED"
    
    with closing(get_connection(TEST_DB_PATH)) as conn:
        assert conn.execute("SELECT COUNT(*) as c FROM verified_events").fetchone()["c"] == 1

def test_approval_uses_latest_verified_context():
    # 1. Setup existing verified position (100% LONG)
    ingest_message("setup", "Buy RELIANCE 100% at CMP", "Equity", TEST_DB_PATH)
    approve_as_parsed("setup", "R", TEST_DB_PATH)
    
    # 2. Ingest two separate reductions
    ingest_message("red1", "REDUCE 50% IN RELIANCE", "Equity", TEST_DB_PATH)
    ingest_message("red2", "REDUCE 50% IN RELIANCE", "Equity", TEST_DB_PATH)
    
    # 3. Approve first one
    approve_as_parsed("red1", "R", TEST_DB_PATH)
    
    v_pos1 = get_pos(TEST_DB_PATH, "verified", "RELIANCE")
    assert float(v_pos1["current_allocation_pct"]) == 50.0
    
    # 4. Approve second one -> should calculate 50% of the *current 50%* = 25%
    approve_as_parsed("red2", "R", TEST_DB_PATH)
    v_pos2 = get_pos(TEST_DB_PATH, "verified", "RELIANCE")
    assert float(v_pos2["current_allocation_pct"]) == 25.0

def test_plain_reduction_with_no_verified_holding_blocked():
    # Ingest a reduction when there's NO position
    res = ingest_message("bad_red", "Book 50% profit in NIFTY at CMP", "Indices", TEST_DB_PATH)
    
    # Attempt to approve
    app_res = approve_as_parsed("bad_red", "R", TEST_DB_PATH)
    assert app_res["status"] == "ERROR"
    assert "MISSING_PRIOR_POSITION" in app_res["message"]
    
    # Should still be pending review
    with closing(get_connection(TEST_DB_PATH)) as conn:
        status = conn.execute("SELECT processing_status FROM incoming_messages WHERE source_message_id='bad_red'").fetchone()[0]
        assert status == "PENDING_REVIEW"

def test_same_message_in_shadow_and_verified_events():
    ingest_message("msg_shared", "Buy NIFTY 100% at CMP", "Indices", TEST_DB_PATH)
    approve_as_parsed("msg_shared", "R", TEST_DB_PATH)
    
    with closing(get_connection(TEST_DB_PATH)) as conn:
        assert conn.execute("SELECT COUNT(*) as c FROM shadow_events WHERE source_message_id='msg_shared'").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) as c FROM verified_events WHERE source_message_id='msg_shared'").fetchone()["c"] == 1

def test_foreign_keys_active():
    with closing(get_connection(TEST_DB_PATH)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO parser_predictions (source_message_id, parser_version, model_version, prediction_json, created_at) VALUES ('invalid', 'v', 'v', '{}', '2020')")

def test_shadow_reduction_uses_verified_baseline_when_shadow_empty(tmp_path):
    csv_path = tmp_path / "initial_positions.csv"
    csv_path.write_text(
        "symbol,direction,current_allocation_pct,market_group\nNIFTY,LONG,100,Indices\n",
        encoding="utf-8",
    )
    assert import_positions(csv_path, TEST_DB_PATH, dry_run=False, apply=True, confirm=True) == 0

    assert get_pos(TEST_DB_PATH, "shadow", "NIFTY") is None
    res = ingest_message("baseline-reduce", "SELL 50% NIFTY", "Indices", TEST_DB_PATH)
    assert res["status"] == "PENDING_REVIEW"

    verified = get_pos(TEST_DB_PATH, "verified", "NIFTY")
    shadow = get_pos(TEST_DB_PATH, "shadow", "NIFTY")
    assert verified["current_allocation_pct"] == "100"
    assert shadow["current_allocation_pct"] == "50.0000000000"

def test_rejected_and_needs_context_reviews_export_as_training_candidates(tmp_path):
    ingest_message("reject-export", "BUY 50% AMD @160", "Equity", TEST_DB_PATH)
    reject("reject-export", "ReviewerA", "bad idea", TEST_DB_PATH)
    ingest_message("context-export", "IF NIFTY BREAKS 26000 BUY", "Indices", TEST_DB_PATH)
    mark_needs_context("context-export", "ReviewerA", "conditional", TEST_DB_PATH)

    output = tmp_path / "verified_shadow_labels.csv"
    export_labels(output, TEST_DB_PATH)
    rows = list(csv.DictReader(output.open(newline="", encoding="utf-8")))

    decisions = {row["source_message_id"]: row["human decision"] for row in rows}
    classifications = {row["source_message_id"]: row["label classification"] for row in rows}
    assert decisions["reject-export"] == "REJECTED"
    assert decisions["context-export"] == "NEEDS_CONTEXT"
    assert classifications["reject-export"] == "human-reviewed training candidate"
    assert classifications["context-export"] == "human-reviewed training candidate"
    assert rows[0]["reviewer"] == "ReviewerA"
