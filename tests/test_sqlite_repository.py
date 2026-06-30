"""
test_sqlite_repository.py
=========================
Tests for SQLite repository implementations.
Tests use an in-memory SQLite DB (:memory:).
"""
import sqlite3
import pytest
from decimal import Decimal

from src.database import _SCHEMA_SQL
from src.schemas import PositionState, ReviewItem
from src.sqlite_repository import (
    SQLitePositionRepository,
    SQLiteTradeEventRepository,
    SQLiteProcessedMessageRepository,
    SQLiteReviewQueueRepository,
    SQLiteSnapshotRepository,
)


@pytest.fixture
def conn():
    """Provide a fresh in-memory SQLite connection with schema."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA_SQL)
    c.commit()
    yield c
    c.close()


class TestPositionRepository:
    def test_insert_and_retrieve(self, conn):
        repo = SQLitePositionRepository(conn)
        pos = PositionState(
            portfolio_id="default",
            symbol="NIFTY",
            direction="LONG",
            current_allocation_pct=Decimal("100"),
            status="OPEN",
        )
        saved = repo.save_position(pos)
        assert saved.position_id is not None

        retrieved = repo.get_position("default", "NIFTY", direction="LONG")
        assert retrieved is not None
        assert retrieved.symbol == "NIFTY"
        assert retrieved.current_allocation_pct == Decimal("100")

    def test_update_position(self, conn):
        repo = SQLitePositionRepository(conn)
        pos = PositionState(portfolio_id="default", symbol="CRUDE", direction="SHORT",
                            current_allocation_pct=Decimal("100"), status="OPEN")
        pos = repo.save_position(pos)

        pos.current_allocation_pct = Decimal("50")
        repo.save_position(pos)

        retrieved = repo.get_position("default", "CRUDE", direction="SHORT")
        assert retrieved.current_allocation_pct == Decimal("50")

    def test_list_open_positions(self, conn):
        repo = SQLitePositionRepository(conn)
        repo.save_position(PositionState(portfolio_id="default", symbol="A",
                                         direction="LONG", current_allocation_pct=Decimal("100"),
                                         status="OPEN"))
        repo.save_position(PositionState(portfolio_id="default", symbol="B",
                                         direction="LONG", current_allocation_pct=Decimal("0"),
                                         status="CLOSED"))
        open_pos = repo.list_open_positions("default")
        assert len(open_pos) == 1
        assert open_pos[0].symbol == "A"

    def test_decimal_precision_preserved(self, conn):
        """Decimal must survive a round-trip through TEXT storage."""
        repo = SQLitePositionRepository(conn)
        pos = PositionState(portfolio_id="default", symbol="NIFTY", direction="LONG",
                            current_allocation_pct=Decimal("12.5"), status="OPEN")
        repo.save_position(pos)
        retrieved = repo.get_position("default", "NIFTY", direction="LONG")
        assert retrieved.current_allocation_pct == Decimal("12.5")


class TestTradeEventRepository:
    def test_insert_and_retrieve(self, conn):
        repo = SQLiteTradeEventRepository(conn)
        record = {
            "source_message_id": "msg-001",
            "processing_order": 1,
            "raw_text": "50% PROFIT BOOK IN NIFTY",
            "normalized_text": "50% PROFIT BOOK IN NIFTY",
            "record_type": "TRADE_ACTION",
            "final_action": "REDUCE_POSITION",
            "symbol": "NIFTY",
            "direction": None,
            "quantity_percent": "50",
            "quantity_basis": "CURRENT_HOLDING",
            "execution_prices_json": "[]",
            "stop_loss": None,
            "targets_json": "[]",
            "model_confidence": 0.99,
            "resolution_source": "RULE",
            "position_before_json": "null",
            "position_after_json": "null",
            "processing_status": "APPLIED",
            "created_at": "2025-01-01T00:00:00Z",
            "processed_at": "2025-01-01T00:00:00Z",
            "parser_version": "v1",
            "parent_source_message_id": None,
            "child_event_index": None,
        }
        event_id = repo.insert_event(record)
        assert event_id > 0

        retrieved = repo.get_event(event_id)
        assert retrieved["final_action"] == "REDUCE_POSITION"
        assert retrieved["symbol"] == "NIFTY"


class TestProcessedMessageRepository:
    def test_mark_and_check(self, conn):
        repo = SQLiteProcessedMessageRepository(conn)
        assert not repo.is_processed("msg-001")
        repo.mark_processed("msg-001", "abc123", "APPLIED", trade_event_id=1)
        assert repo.is_processed("msg-001")

    def test_idempotent_mark(self, conn):
        repo = SQLiteProcessedMessageRepository(conn)
        repo.mark_processed("msg-001", "abc", "APPLIED")
        # Should not raise on duplicate
        repo.mark_processed("msg-001", "abc", "APPLIED")
        assert repo.is_processed("msg-001")


class TestReviewQueueRepository:
    def test_add_and_list(self, conn):
        repo = SQLiteReviewQueueRepository(conn)
        item = ReviewItem(
            source_message_id="msg-001",
            raw_text="SELL 50%",
            parsed_event_json="{}",
            review_reason="AMBIGUOUS",
            review_status="PENDING",
        )
        item_id = repo.add_review_item(item)
        assert item_id > 0

        pending = repo.list_pending()
        assert len(pending) == 1
        assert pending[0].review_reason == "AMBIGUOUS"

    def test_update_status(self, conn):
        repo = SQLiteReviewQueueRepository(conn)
        item = ReviewItem(source_message_id="msg-002", raw_text="x",
                          parsed_event_json="{}", review_reason="TEST",
                          review_status="PENDING")
        item_id = repo.add_review_item(item)
        repo.update_status(item_id, "APPROVED", notes="Looks good.")

        all_items = repo.list_all()
        updated = next(i for i in all_items if i.id == item_id)
        assert updated.review_status == "APPROVED"
        assert updated.resolution_notes == "Looks good."
