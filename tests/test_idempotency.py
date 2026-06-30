"""
test_idempotency.py
===================
Tests that the same source_message_id cannot modify holdings twice.
"""
import pytest
from decimal import Decimal

from src.schemas import ParsedTradeEvent
from tests.test_holdings_engine import (
    MemEventRepo, MemPosRepo, MemProcessedRepo, MemReviewRepo, MemSnapshotRepo,
    make_engine, make_event,
)


class TestIdempotency:
    def test_same_message_id_twice(self):
        engine, pos_repo, review_repo = make_engine()

        event = make_event(
            source_message_id="idempotent-001",
            final_action="OPEN_LONG",
            symbol="NIFTY",
            quantity_percent=Decimal("100"),
        )
        r1 = engine.apply_event(event)
        assert r1.status == "OK"

        r2 = engine.apply_event(event)
        assert r2.status == "ALREADY_PROCESSED"

        # Position must not have been modified twice
        positions = [p for p in pos_repo.list_open_positions() if p.symbol == "NIFTY"]
        assert len(positions) == 1
        assert positions[0].current_allocation_pct == Decimal("100")

    def test_different_message_ids_both_applied(self):
        engine, pos_repo, _ = make_engine()

        e1 = make_event(source_message_id="msg-A", final_action="OPEN_LONG",
                        symbol="COPPER", quantity_percent=Decimal("100"))
        e2 = make_event(source_message_id="msg-B", final_action="REDUCE_POSITION",
                        symbol="COPPER", quantity_percent=Decimal("50"),
                        quantity_basis="CURRENT_HOLDING")

        engine.apply_event(e1)
        engine.apply_event(e2)

        pos = pos_repo.get_position("default", "COPPER", "LONG")
        assert pos.current_allocation_pct == Decimal("50")

    def test_none_message_id_does_not_block(self):
        """Events with no message ID cannot be idempotency-checked."""
        engine, pos_repo, _ = make_engine()

        e = make_event(source_message_id=None, final_action="OPEN_LONG",
                       symbol="SNDK", quantity_percent=Decimal("100"))
        r = engine.apply_event(e)
        assert r.status == "OK"
