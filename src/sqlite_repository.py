"""
sqlite_repository.py
====================
Concrete SQLite implementations of the repository interfaces.

IMPORTANT: Decimal values are persisted as TEXT strings and converted back
           to Decimal on read. Never store Decimal as REAL (floating point).

All repositories receive a live sqlite3.Connection.
Callers are responsible for transaction management (commit / rollback).

Inject these into the HoldingsEngine:
    engine = HoldingsEngine(
        position_repo=SQLitePositionRepository(conn),
        event_repo=SQLiteTradeEventRepository(conn),
        processed_repo=SQLiteProcessedMessageRepository(conn),
        review_repo=SQLiteReviewQueueRepository(conn),
        snapshot_repo=SQLiteSnapshotRepository(conn),
    )
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.schemas import PositionState, ReviewItem


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d(value) -> Optional[Decimal]:
    """Convert a value to Decimal or return None."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _row_to_position(row: sqlite3.Row) -> PositionState:
    """Convert a database row to a PositionState."""
    targets_raw = row["targets_json"] or "[]"
    try:
        targets_list = [Decimal(str(t)) for t in json.loads(targets_raw)]
    except Exception:
        targets_list = []

    return PositionState(
        position_id=row["id"],
        portfolio_id=row["portfolio_id"],
        market_group=row["market_group"],
        symbol=row["symbol"],
        contract_month=row["contract_month"],
        option_type=row["option_type"],
        strike_price=_d(row["strike_price"]),
        direction=row["direction"],
        current_allocation_pct=Decimal(str(row["current_allocation_pct"] or "0")),
        average_entry_price=_d(row["average_entry_price"]),
        stop_loss=_d(row["stop_loss"]),
        targets=targets_list,
        status=row["status"],
        opened_at=row["opened_at"],
        updated_at=row["updated_at"],
        version=row["version"],
    )


# ---------------------------------------------------------------------------
# SQLitePositionRepository
# ---------------------------------------------------------------------------
class SQLitePositionRepository:
    """Read and write position state to the positions table."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_position(
        self,
        portfolio_id: str,
        symbol: str,
        direction: Optional[str] = None,
        market_group: Optional[str] = None,
        contract_month: Optional[str] = None,
        option_type: Optional[str] = None,
        strike_price=None,
    ) -> Optional[PositionState]:
        params: list = [portfolio_id, symbol]
        sql = """
            SELECT * FROM positions
            WHERE portfolio_id = ? AND symbol = ?
        """
        if direction:
            sql += " AND direction = ?"
            params.append(direction)
        sql += " ORDER BY id DESC LIMIT 1"

        row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return _row_to_position(row)

    def save_position(self, position: PositionState) -> PositionState:
        targets_json = json.dumps([str(t) for t in position.targets])
        if position.position_id is None:
            # Insert new position
            cursor = self._conn.execute(
                """
                INSERT INTO positions (
                    portfolio_id, market_group, symbol, contract_month,
                    option_type, strike_price, direction,
                    current_allocation_pct, average_entry_price, stop_loss,
                    targets_json, status, opened_at, updated_at, version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    position.portfolio_id,
                    position.market_group,
                    position.symbol,
                    position.contract_month,
                    position.option_type,
                    str(position.strike_price) if position.strike_price else None,
                    position.direction,
                    str(position.current_allocation_pct),
                    str(position.average_entry_price) if position.average_entry_price else None,
                    str(position.stop_loss) if position.stop_loss else None,
                    targets_json,
                    position.status,
                    position.opened_at,
                    position.updated_at,
                    position.version,
                ),
            )
            position.position_id = cursor.lastrowid
        else:
            self._conn.execute(
                """
                UPDATE positions SET
                    market_group=?, symbol=?, contract_month=?,
                    option_type=?, strike_price=?, direction=?,
                    current_allocation_pct=?, average_entry_price=?,
                    stop_loss=?, targets_json=?, status=?,
                    updated_at=?, version=?
                WHERE id=?
                """,
                (
                    position.market_group,
                    position.symbol,
                    position.contract_month,
                    position.option_type,
                    str(position.strike_price) if position.strike_price else None,
                    position.direction,
                    str(position.current_allocation_pct),
                    str(position.average_entry_price) if position.average_entry_price else None,
                    str(position.stop_loss) if position.stop_loss else None,
                    targets_json,
                    position.status,
                    position.updated_at,
                    position.version,
                    position.position_id,
                ),
            )
        return position

    def list_open_positions(self, portfolio_id: str = "default") -> list[PositionState]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE portfolio_id=? AND status='OPEN' ORDER BY id",
            (portfolio_id,),
        ).fetchall()
        return [_row_to_position(r) for r in rows]

    def list_all_positions(self, portfolio_id: str = "default") -> list[PositionState]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE portfolio_id=? ORDER BY id",
            (portfolio_id,),
        ).fetchall()
        return [_row_to_position(r) for r in rows]


# ---------------------------------------------------------------------------
# SQLiteTradeEventRepository
# ---------------------------------------------------------------------------
class SQLiteTradeEventRepository:
    """Immutable trade-event ledger."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_event(self, event_record: dict) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO trade_events (
                source_message_id, processing_order, raw_text, normalized_text,
                record_type, final_action, symbol, direction, quantity_percent,
                quantity_basis, execution_prices_json, stop_loss, targets_json,
                model_confidence, resolution_source, position_before_json,
                position_after_json, processing_status, created_at, processed_at,
                parser_version, parent_source_message_id, child_event_index
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_record.get("source_message_id"),
                event_record.get("processing_order"),
                event_record.get("raw_text"),
                event_record.get("normalized_text"),
                event_record.get("record_type"),
                event_record.get("final_action"),
                event_record.get("symbol"),
                event_record.get("direction"),
                event_record.get("quantity_percent"),
                event_record.get("quantity_basis"),
                event_record.get("execution_prices_json", "[]"),
                event_record.get("stop_loss"),
                event_record.get("targets_json", "[]"),
                event_record.get("model_confidence"),
                event_record.get("resolution_source"),
                event_record.get("position_before_json"),
                event_record.get("position_after_json"),
                event_record.get("processing_status"),
                event_record.get("created_at"),
                event_record.get("processed_at"),
                event_record.get("parser_version"),
                event_record.get("parent_source_message_id"),
                event_record.get("child_event_index"),
            ),
        )
        return cursor.lastrowid

    def get_event(self, event_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM trade_events WHERE id=?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_events_for_symbol(self, symbol: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trade_events WHERE symbol=? ORDER BY id", (symbol,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_all_events(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trade_events ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# SQLiteProcessedMessageRepository
# ---------------------------------------------------------------------------
class SQLiteProcessedMessageRepository:
    """Idempotency log – prevents processing the same message twice."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def mark_processed(
        self,
        source_message_id: str,
        text_hash: str,
        processing_status: str,
        trade_event_id: Optional[int] = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO processed_messages
                (source_message_id, text_hash, processed_at, processing_status, trade_event_id)
            VALUES (?,?,?,?,?)
            """,
            (source_message_id, text_hash, _now(), processing_status, trade_event_id),
        )

    def is_processed(self, source_message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_messages WHERE source_message_id=?",
            (source_message_id,),
        ).fetchone()
        return row is not None

    def get_status(self, source_message_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT processing_status FROM processed_messages WHERE source_message_id=?",
            (source_message_id,),
        ).fetchone()
        return row["processing_status"] if row else None


# ---------------------------------------------------------------------------
# SQLiteReviewQueueRepository
# ---------------------------------------------------------------------------
class SQLiteReviewQueueRepository:
    """Manual review queue."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add_review_item(self, item: ReviewItem) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO manual_review_queue
                (source_message_id, raw_text, parsed_event_json, review_reason,
                 review_status, created_at, resolved_at, resolution_notes)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                item.source_message_id,
                item.raw_text,
                item.parsed_event_json,
                item.review_reason,
                item.review_status,
                item.created_at or _now(),
                item.resolved_at,
                item.resolution_notes,
            ),
        )
        return cursor.lastrowid

    def list_pending(self) -> list[ReviewItem]:
        rows = self._conn.execute(
            "SELECT * FROM manual_review_queue WHERE review_status='PENDING' ORDER BY id"
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def list_all(self) -> list[ReviewItem]:
        rows = self._conn.execute(
            "SELECT * FROM manual_review_queue ORDER BY id"
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def update_status(
        self,
        item_id: int,
        status: str,
        notes: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE manual_review_queue
            SET review_status=?, resolution_notes=?, resolved_at=?
            WHERE id=?
            """,
            (status, notes, _now(), item_id),
        )

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> ReviewItem:
        return ReviewItem(
            id=row["id"],
            source_message_id=row["source_message_id"],
            raw_text=row["raw_text"],
            parsed_event_json=row["parsed_event_json"] or "",
            review_reason=row["review_reason"] or "",
            review_status=row["review_status"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            resolution_notes=row["resolution_notes"],
        )


# ---------------------------------------------------------------------------
# SQLiteSnapshotRepository
# ---------------------------------------------------------------------------
class SQLiteSnapshotRepository:
    """Holdings snapshots after each applied event."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save_snapshot(
        self,
        trade_event_id: int,
        processing_order: int,
        portfolio_id: str,
        positions_json: str,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO position_snapshots
                (trade_event_id, processing_order, portfolio_id, positions_json, created_at)
            VALUES (?,?,?,?,?)
            """,
            (trade_event_id, processing_order, portfolio_id, positions_json, _now()),
        )
        return cursor.lastrowid

    def list_snapshots(self, portfolio_id: str = "default") -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM position_snapshots WHERE portfolio_id=? ORDER BY id",
            (portfolio_id,),
        ).fetchall()
        return [dict(r) for r in rows]
