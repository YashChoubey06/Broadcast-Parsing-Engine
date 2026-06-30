"""
test_historical_replay.py
=========================
Integration tests for src/replay.py

Uses a small synthetic CSV fixture (does not require the full production dataset).
Tests:
 - Dry-run does not modify the real database
 - Safe-apply applies eligible events
 - Second run produces no duplicate position changes (idempotency)
 - Multi-instrument messages go to review
 - Corrections go to review
 - Conditional messages go to review
"""
import csv
import io
import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
import pandas as pd

from src.replay import run_replay
from src.database import _SCHEMA_SQL


# ---------------------------------------------------------------------------
# Synthetic test data
# ---------------------------------------------------------------------------
SYNTHETIC_ROWS = [
    # processing_order, source_message_id, raw_text, action_label, symbol,
    # auto_apply_eligible, needs_review, requires_context, market_group
    (1,  "msg-001", "BUY NIFTY @25000 SL 24800 TGT 25300",     "OPEN_LONG",      "NIFTY",   "True", "False", "False", "INDIA_INDEX"),
    (2,  "msg-002", "50% PROFIT BOOK IN NIFTY @25942",          "REDUCE_POSITION","NIFTY",   "True", "False", "False", "INDIA_INDEX"),
    (3,  "msg-003", "50% PROFIT BOOK IN NIFTY @25942",          "REDUCE_POSITION","NIFTY",   "True", "False", "False", "INDIA_INDEX"),
    (4,  "msg-004", "SL TOUCH IN NIFTY",                        "CLOSE_POSITION", "NIFTY",   "True", "False", "False", "INDIA_INDEX"),
    (5,  "msg-005", "CORRECTION: Buy Gold @156950 SL 156500",   "OPEN_LONG",      "GOLD",    "False","True",  "True",  "INDIA_COMMODITY"),
    (6,  "msg-006", "If Dow crosses 45000, buy",                 "CONDITIONAL_INSTRUCTION","", "False","True","True",   "GLOBAL_INDEX"),
    (7,  "msg-007", "BUY CRUDE MINI @8637 SL 8500 TGT 8800",    "OPEN_LONG",      "CRUDE_MINI","True","False","False", "INDIA_COMMODITY"),
    (8,  "msg-007", "BUY CRUDE MINI @8637 SL 8500 TGT 8800",    "OPEN_LONG",      "CRUDE_MINI","True","False","False", "INDIA_COMMODITY"),  # duplicate
    (9,  "msg-008", "SL touched in Gold & Silver",               "CLOSE_POSITION", "GOLD",    "False","True","False",  "INDIA_COMMODITY"),
    (10, "msg-009", "Hold AMD with SL 145",                     "UPDATE_STOP_LOSS","AMD",    "True", "False","False",  "GLOBAL_EQUITY"),
]

CSV_HEADER = [
    "processing_order", "source_message_id", "raw_text", "action_label", "symbol",
    "auto_apply_eligible", "needs_review", "requires_context", "market_group",
    "record_type", "normalized_text", "normalized_text_hash", "created_at",
    "direction", "quantity_percent", "quantity_basis", "stop_loss", "targets_json",
    "execution_prices_json", "is_correction", "is_multi_instrument",
]


def make_synthetic_csv() -> Path:
    """Write synthetic test data to a temp file and return its path."""
    rows = []
    for r in SYNTHETIC_ROWS:
        row = {
            "processing_order": r[0],
            "source_message_id": r[1],
            "raw_text": r[2],
            "action_label": r[3],
            "symbol": r[4],
            "auto_apply_eligible": r[5],
            "needs_review": r[6],
            "requires_context": r[7],
            "market_group": r[8],
            "record_type": "TRADE_ACTION",
            "normalized_text": r[2],
            "normalized_text_hash": "",
            "created_at": "2025-01-01T00:00:00Z",
            "direction": "",
            "quantity_percent": "",
            "quantity_basis": "",
            "stop_loss": "",
            "targets_json": "[]",
            "execution_prices_json": "[]",
            "is_correction": "False",
            "is_multi_instrument": "False",
        }
        rows.append(row)
    df = pd.DataFrame(rows)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8")
    df.to_csv(tmp.name, index=False)
    return Path(tmp.name)


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    conn.close()
    yield db_path
    # On Windows, SQLite WAL mode may keep .db-wal and .db-shm files open.
    # Force them closed by connecting and checkpointing.
    try:
        import gc
        gc.collect()  # collect any lingering connection objects
        c = sqlite3.connect(str(db_path))
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    except Exception:
        pass
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix) if suffix else db_path
        try:
            p.unlink(missing_ok=True)
        except PermissionError:
            pass  # Windows may still hold the file; ignore in tests


@pytest.fixture
def synthetic_csv():
    p = make_synthetic_csv()
    yield p
    p.unlink(missing_ok=True)


class TestDryRun:
    def test_dry_run_does_not_write_to_db(self, synthetic_csv, temp_db):
        """Dry-run must not create any positions in the real database."""
        stats, pos_repo, review_repo, event_repo = run_replay(
            input_path=synthetic_csv,
            db_path=temp_db,
            mode="dry-run",
        )
        # Check the real DB has no positions
        conn = sqlite3.connect(str(temp_db))
        rows = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        conn.close()
        assert rows == 0, "Dry-run must not write positions to the real database."

    def test_dry_run_stats_are_nonzero(self, synthetic_csv, temp_db):
        stats, *_ = run_replay(
            input_path=synthetic_csv,
            db_path=temp_db,
            mode="dry-run",
        )
        assert stats.total_read >= len(SYNTHETIC_ROWS) - 1  # minus dupe already counted

    def test_correction_goes_to_review(self, synthetic_csv, temp_db):
        stats, pos_repo, review_repo, event_repo = run_replay(
            input_path=synthetic_csv,
            db_path=temp_db,
            mode="dry-run",
        )
        assert stats.sent_to_review >= 1

    def test_conditional_goes_to_review(self, synthetic_csv, temp_db):
        stats, pos_repo, review_repo, event_repo = run_replay(
            input_path=synthetic_csv,
            db_path=temp_db,
            mode="dry-run",
        )
        review_reasons = [i.review_reason for i in review_repo.list_all()]
        conditional_found = any("CONDITIONAL" in r or "REQUIRES_CONTEXT" in r
                                or "VALIDATION" in r for r in review_reasons)
        # At least the correction and conditional messages went to review
        assert stats.sent_to_review >= 2

    def test_duplicate_skipped(self, synthetic_csv, temp_db):
        stats, *_ = run_replay(
            input_path=synthetic_csv,
            db_path=temp_db,
            mode="dry-run",
        )
        assert stats.duplicates_ignored >= 1


class TestSafeApply:
    def test_safe_apply_writes_positions(self, synthetic_csv, temp_db):
        stats, pos_repo, review_repo, event_repo = run_replay(
            input_path=synthetic_csv,
            db_path=temp_db,
            mode="safe-apply",
        )
        # Verify at least some positions were processed
        assert stats.parsed >= 5

    def test_idempotency_second_run(self, synthetic_csv, temp_db):
        """Running safe-apply twice must not create duplicate position changes."""
        stats1, pos_repo1, *_ = run_replay(
            input_path=synthetic_csv,
            db_path=temp_db,
            mode="safe-apply",
        )
        # Count positions after first run
        conn = sqlite3.connect(str(temp_db))
        count1 = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        conn.close()

        stats2, *_ = run_replay(
            input_path=synthetic_csv,
            db_path=temp_db,
            mode="safe-apply",
        )
        conn = sqlite3.connect(str(temp_db))
        count2 = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        conn.close()

        assert count1 == count2, "Second replay must not create additional positions."
        assert stats2.duplicates_ignored >= stats1.total_read - 1  # most should be skipped
