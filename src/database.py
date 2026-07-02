"""
database.py
===========
SQLite connection management and schema initialisation.

The database file is created at:
    storage/trade_holdings.db

Run this module directly to initialise:
    python -m src.database

IMPORTANT: Decimal values are stored as TEXT (not REAL) to preserve
           exact decimal string representation.
           Example: "12.5000000000" not 12.5
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager, closing
from pathlib import Path
from typing import Iterator

from src.config import DATABASE_PATH, STORAGE_DIR


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
-- Positions: latest state of each position
CREATE TABLE IF NOT EXISTS positions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id            TEXT    NOT NULL DEFAULT 'default',
    market_group            TEXT    NOT NULL DEFAULT 'UNKNOWN',
    symbol                  TEXT    NOT NULL,
    contract_month          TEXT    NOT NULL DEFAULT '',
    option_type             TEXT    NOT NULL DEFAULT '',
    strike_price            TEXT    NOT NULL DEFAULT '',
    direction               TEXT    NOT NULL DEFAULT '',
    current_allocation_pct  TEXT    NOT NULL DEFAULT '0',
    average_entry_price     TEXT,
    stop_loss               TEXT,
    targets_json            TEXT    NOT NULL DEFAULT '[]',
    status                  TEXT    NOT NULL DEFAULT 'OPEN',
    opened_at               TEXT,
    updated_at              TEXT,
    version                 INTEGER NOT NULL DEFAULT 1
);

-- Immutable trade-event ledger
CREATE TABLE IF NOT EXISTS trade_events (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_message_id           TEXT,
    processing_order            INTEGER,
    raw_text                    TEXT,
    normalized_text             TEXT,
    record_type                 TEXT,
    final_action                TEXT,
    symbol                      TEXT,
    direction                   TEXT,
    quantity_percent            TEXT,
    quantity_basis              TEXT,
    execution_prices_json       TEXT    NOT NULL DEFAULT '[]',
    stop_loss                   TEXT,
    targets_json                TEXT    NOT NULL DEFAULT '[]',
    model_confidence            REAL,
    resolution_source           TEXT,
    position_before_json        TEXT,
    position_after_json         TEXT,
    processing_status           TEXT,
    created_at                  TEXT,
    processed_at                TEXT,
    parser_version              TEXT,
    parent_source_message_id    TEXT,
    child_event_index           INTEGER
);

-- Idempotency log
CREATE TABLE IF NOT EXISTS processed_messages (
    source_message_id   TEXT    PRIMARY KEY,
    text_hash           TEXT,
    processed_at        TEXT,
    processing_status   TEXT,
    trade_event_id      INTEGER
);

-- Manual review queue
CREATE TABLE IF NOT EXISTS manual_review_queue (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_message_id   TEXT,
    raw_text            TEXT,
    parsed_event_json   TEXT,
    review_reason       TEXT,
    review_status       TEXT    NOT NULL DEFAULT 'PENDING',
    created_at          TEXT,
    resolved_at         TEXT,
    resolution_notes    TEXT
);

-- Position snapshots after each applied event
CREATE TABLE IF NOT EXISTS position_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_event_id      INTEGER,
    processing_order    INTEGER,
    portfolio_id        TEXT,
    positions_json      TEXT,
    created_at          TEXT
);

-- Useful indexes
CREATE INDEX IF NOT EXISTS idx_pos_symbol ON positions(symbol, direction, status);
CREATE INDEX IF NOT EXISTS idx_pos_full_identity ON positions(
    portfolio_id, market_group, symbol, contract_month,
    option_type, strike_price, direction, status
);
CREATE INDEX IF NOT EXISTS idx_events_symbol ON trade_events(symbol);
CREATE INDEX IF NOT EXISTS idx_events_order ON trade_events(processing_order);
CREATE INDEX IF NOT EXISTS idx_review_status ON manual_review_queue(review_status);
"""


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------
def get_connection(db_path: Path = DATABASE_PATH) -> sqlite3.Connection:
    """
    Open a SQLite connection with sensible defaults.

    Foreign keys are enabled.
    WAL mode for better concurrent read performance.
    """
    conn = sqlite3.connect(str(db_path), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(db_path: Path = DATABASE_PATH) -> Iterator[sqlite3.Connection]:
    """
    Context manager providing an atomic transaction.

    On success → commit.
    On any exception → rollback.

    Usage::

        with transaction() as conn:
            conn.execute(...)
            # ... more statements ...
            # automatically committed on exit
    """
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------
def init_db(db_path: Path = DATABASE_PATH) -> None:
    """
    Create the database and all tables if they do not exist.
    Safe to call on an already-initialised database (idempotent).
    """
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with closing(get_connection(db_path)) as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
    print(f"Database initialised: {db_path}")


def drop_all_tables(db_path: Path = DATABASE_PATH) -> None:
    """
    Drop all tables – development use only.
    Called by the --reset-database command after explicit confirmation.
    """
    drop_sql = """
    DROP TABLE IF EXISTS position_snapshots;
    DROP TABLE IF EXISTS manual_review_queue;
    DROP TABLE IF EXISTS processed_messages;
    DROP TABLE IF EXISTS trade_events;
    DROP TABLE IF EXISTS positions;
    """
    with get_connection(db_path) as conn:
        conn.executescript(drop_sql)
        conn.commit()
    print(f"All tables dropped: {db_path}")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
