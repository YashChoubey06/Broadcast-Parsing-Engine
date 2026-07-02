"""
test_holdings_engine.py
=======================
Tests for src/holdings_engine.py — all required scenarios from the spec.

All holdings arithmetic uses Decimal for exact results.
"""
import pytest
from decimal import Decimal

from src.holdings_engine import HoldingsEngine
from src.schemas import ParsedTradeEvent, PositionState


# ---------------------------------------------------------------------------
# In-memory repository fixtures (no SQLite needed for engine tests)
# ---------------------------------------------------------------------------
class MemPosRepo:
    def __init__(self):
        self._positions: dict = {}
        self._next_id = 1

    def get_position(self, portfolio_id, symbol, direction=None, **kwargs):
        for pos in sorted(self._positions.values(), key=lambda p: p.position_id or 0, reverse=True):
            if pos.portfolio_id == portfolio_id and pos.symbol == symbol:
                if direction is None or pos.direction == direction:
                    return pos
        return None

    def save_position(self, position):
        if position.position_id is None:
            position.position_id = self._next_id
            self._next_id += 1
        self._positions[position.position_id] = position
        return position

    def list_open_positions(self, portfolio_id="default"):
        return [p for p in self._positions.values()
                if p.portfolio_id == portfolio_id and p.status == "OPEN"]

    def list_all_positions(self, portfolio_id="default"):
        return [p for p in self._positions.values() if p.portfolio_id == portfolio_id]


class MemEventRepo:
    def __init__(self):
        self._events = []
        self._next_id = 1

    def insert_event(self, record):
        record["id"] = self._next_id
        self._events.append(dict(record))
        self._next_id += 1
        return record["id"]

    def get_event(self, event_id):
        return next((e for e in self._events if e.get("id") == event_id), None)

    def list_events_for_symbol(self, symbol):
        return [e for e in self._events if e.get("symbol") == symbol]

    def list_all_events(self):
        return list(self._events)


class MemProcessedRepo:
    def __init__(self):
        self._seen: dict = {}

    def mark_processed(self, source_message_id, text_hash, processing_status, trade_event_id=None):
        self._seen[source_message_id] = processing_status

    def is_processed(self, source_message_id):
        return source_message_id in self._seen

    def get_status(self, source_message_id):
        return self._seen.get(source_message_id)


class MemReviewRepo:
    def __init__(self):
        self._items = []
        self._next_id = 1

    def add_review_item(self, item):
        item.id = self._next_id
        self._items.append(item)
        self._next_id += 1
        return item.id

    def list_pending(self):
        return [i for i in self._items if i.review_status == "PENDING"]

    def list_all(self):
        return list(self._items)

    def update_status(self, item_id, status, notes=None):
        pass


class MemSnapshotRepo:
    def __init__(self):
        self._snaps = []

    def save_snapshot(self, trade_event_id, processing_order, portfolio_id, positions_json):
        self._snaps.append({
            "trade_event_id": trade_event_id,
            "processing_order": processing_order,
            "portfolio_id": portfolio_id,
        })
        return len(self._snaps)

    def list_snapshots(self, portfolio_id="default"):
        return self._snaps


def make_engine():
    pos_repo = MemPosRepo()
    event_repo = MemEventRepo()
    processed_repo = MemProcessedRepo()
    review_repo = MemReviewRepo()
    snapshot_repo = MemSnapshotRepo()
    engine = HoldingsEngine(
        position_repo=pos_repo,
        event_repo=event_repo,
        processed_repo=processed_repo,
        review_repo=review_repo,
        snapshot_repo=snapshot_repo,
    )
    return engine, pos_repo, review_repo


def make_event(**kwargs) -> ParsedTradeEvent:
    defaults = {
        "source_message_id": "test-001",
        "raw_text": "TEST",
        "normalized_text": "TEST",
        "final_action": "OPEN_LONG",
        "symbol": "NIFTY",
        "direction": "LONG",
        "quantity_percent": Decimal("100"),
        "quantity_basis": "MODEL_ALLOCATION",
        "auto_apply_eligible": True,
        "needs_review": False,
        "record_type": "TRADE_ACTION",
    }
    defaults.update(kwargs)
    return ParsedTradeEvent(**defaults)


# ---------------------------------------------------------------------------
# Test: Current-holding reduction chain
# ---------------------------------------------------------------------------
class TestReductionChain:
    def test_chain_100_to_50_to_25_to_12_5(self):
        engine, pos_repo, _ = make_engine()

        # Open 100%
        r = engine.apply_event(make_event(source_message_id="open-001", final_action="OPEN_LONG",
                                          quantity_percent=Decimal("100")))
        assert r.status == "OK"
        pos = pos_repo.get_position("default", "NIFTY", "LONG")
        assert pos.current_allocation_pct == Decimal("100")

        # Sell 50% → 50 remains
        r = engine.apply_event(make_event(source_message_id="reduce-001", final_action="REDUCE_POSITION",
                                          quantity_percent=Decimal("50"), quantity_basis="CURRENT_HOLDING"))
        assert r.status == "OK"
        assert pos_repo.get_position("default", "NIFTY", "LONG").current_allocation_pct == Decimal("50")

        # Sell 50% again → 25 remains
        r = engine.apply_event(make_event(source_message_id="reduce-002", final_action="REDUCE_POSITION",
                                          quantity_percent=Decimal("50"), quantity_basis="CURRENT_HOLDING"))
        assert r.status == "OK"
        assert pos_repo.get_position("default", "NIFTY", "LONG").current_allocation_pct == Decimal("25")

        # Sell 50% again → 12.5 remains
        r = engine.apply_event(make_event(source_message_id="reduce-003", final_action="REDUCE_POSITION",
                                          quantity_percent=Decimal("50"), quantity_basis="CURRENT_HOLDING"))
        assert r.status == "OK"
        assert pos_repo.get_position("default", "NIFTY", "LONG").current_allocation_pct == Decimal("12.5")


class TestEntryAdditionSemantics:
    def test_buy_50_sequence_reaches_150(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="buy-1", final_action="OPEN_LONG",
                                      symbol="TSLA", quantity_percent=Decimal("50"),
                                      quantity_basis="CUSTOMER_BUYING_CAPACITY"))
        assert pos_repo.get_position("default", "TSLA", "LONG").current_allocation_pct == Decimal("50")

        engine.apply_event(make_event(source_message_id="buy-2", final_action="OPEN_LONG",
                                      symbol="TSLA", quantity_percent=Decimal("50"),
                                      quantity_basis="CUSTOMER_BUYING_CAPACITY"))
        assert pos_repo.get_position("default", "TSLA", "LONG").current_allocation_pct == Decimal("100")

        engine.apply_event(make_event(source_message_id="buy-3", final_action="OPEN_LONG",
                                      symbol="TSLA", quantity_percent=Decimal("50"),
                                      quantity_basis="CUSTOMER_BUYING_CAPACITY"))
        assert pos_repo.get_position("default", "TSLA", "LONG").current_allocation_pct == Decimal("150")

    def test_sell_50_sequence_reaches_150(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="sell-1", final_action="OPEN_SHORT",
                                      symbol="TSLA", direction="SHORT",
                                      quantity_percent=Decimal("50"),
                                      quantity_basis="CUSTOMER_BUYING_CAPACITY"))
        assert pos_repo.get_position("default", "TSLA", "SHORT").current_allocation_pct == Decimal("50")

        engine.apply_event(make_event(source_message_id="sell-2", final_action="OPEN_SHORT",
                                      symbol="TSLA", direction="SHORT",
                                      quantity_percent=Decimal("50"),
                                      quantity_basis="CUSTOMER_BUYING_CAPACITY"))
        assert pos_repo.get_position("default", "TSLA", "SHORT").current_allocation_pct == Decimal("100")

        engine.apply_event(make_event(source_message_id="sell-3", final_action="OPEN_SHORT",
                                      symbol="TSLA", direction="SHORT",
                                      quantity_percent=Decimal("50"),
                                      quantity_basis="CUSTOMER_BUYING_CAPACITY"))
        assert pos_repo.get_position("default", "TSLA", "SHORT").current_allocation_pct == Decimal("150")

    def test_plain_stock_buy_adds_100_each_time(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="buy-nvda-1", final_action="OPEN_LONG",
                                      symbol="NVDA", quantity_percent=None,
                                      quantity_basis="CUSTOMER_BUYING_CAPACITY"))
        engine.apply_event(make_event(source_message_id="buy-nvda-2", final_action="OPEN_LONG",
                                      symbol="NVDA", quantity_percent=None,
                                      quantity_basis="CUSTOMER_BUYING_CAPACITY"))

        assert pos_repo.get_position("default", "NVDA", "LONG").current_allocation_pct == Decimal("200.0")

    def test_plain_stock_sell_adds_100_each_time(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="sell-infy-1", final_action="OPEN_SHORT",
                                      symbol="INFY", direction="SHORT",
                                      quantity_percent=None,
                                      quantity_basis="CUSTOMER_BUYING_CAPACITY"))
        engine.apply_event(make_event(source_message_id="sell-infy-2", final_action="OPEN_SHORT",
                                      symbol="INFY", direction="SHORT",
                                      quantity_percent=None,
                                      quantity_basis="CUSTOMER_BUYING_CAPACITY"))

        assert pos_repo.get_position("default", "INFY", "SHORT").current_allocation_pct == Decimal("200.0")


# ---------------------------------------------------------------------------
# Test: Part profit (25% reductions)
# ---------------------------------------------------------------------------
class TestPartProfit:
    def test_part_profit_chain(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="open-001", final_action="OPEN_LONG",
                                      symbol="NVDA", quantity_percent=Decimal("100")))

        engine.apply_event(make_event(source_message_id="pp-001", final_action="REDUCE_POSITION",
                                      symbol="NVDA", quantity_percent=Decimal("25"),
                                      quantity_basis="CURRENT_HOLDING"))
        pos = pos_repo.get_position("default", "NVDA", "LONG")
        assert pos.current_allocation_pct == Decimal("75")

        engine.apply_event(make_event(source_message_id="pp-002", final_action="REDUCE_POSITION",
                                      symbol="NVDA", quantity_percent=Decimal("25"),
                                      quantity_basis="CURRENT_HOLDING"))
        pos = pos_repo.get_position("default", "NVDA", "LONG")
        assert pos.current_allocation_pct == Decimal("56.25")


# ---------------------------------------------------------------------------
# Test: Full close
# ---------------------------------------------------------------------------
class TestFullClose:
    def test_close_position(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="open-001", final_action="OPEN_LONG",
                                      symbol="INFY", quantity_percent=Decimal("50")))
        engine.apply_event(make_event(source_message_id="close-001", final_action="CLOSE_POSITION",
                                      symbol="INFY", quantity_percent=Decimal("100"),
                                      quantity_basis="CURRENT_HOLDING"))
        pos = pos_repo.get_position("default", "INFY", "LONG")
        assert pos.current_allocation_pct == Decimal("0")
        assert pos.status == "CLOSED"


# ---------------------------------------------------------------------------
# Test: SL touch close
# ---------------------------------------------------------------------------
class TestSLTouchClose:
    def test_sl_touch_closes(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="open-001", final_action="OPEN_LONG",
                                      symbol="GOLD"))
        engine.apply_event(make_event(source_message_id="sl-001", final_action="CLOSE_POSITION",
                                      symbol="GOLD", quantity_percent=Decimal("100"),
                                      quantity_basis="CURRENT_HOLDING",
                                      remaining_holding_multiplier=Decimal("0")))
        pos = pos_repo.get_position("default", "GOLD", "LONG")
        assert pos.status == "CLOSED"
        assert pos.current_allocation_pct == Decimal("0")


# ---------------------------------------------------------------------------
# Test: Hold (no change)
# ---------------------------------------------------------------------------
class TestHold:
    def test_hold_does_not_change_allocation(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="open-001", final_action="OPEN_LONG",
                                      symbol="AMD", quantity_percent=Decimal("75")))
        engine.apply_event(make_event(source_message_id="hold-001", final_action="HOLD_POSITION",
                                      symbol="AMD", quantity_percent=None,
                                      quantity_basis="NONE"))
        pos = pos_repo.get_position("default", "AMD", "LONG")
        assert pos.current_allocation_pct == Decimal("75")


# ---------------------------------------------------------------------------
# Test: Stop-loss update
# ---------------------------------------------------------------------------
class TestStopLossUpdate:
    def test_sl_update(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="open-001", final_action="OPEN_LONG",
                                      symbol="NIFTY", stop_loss=Decimal("24000")))
        engine.apply_event(make_event(source_message_id="sl-upd-001", final_action="UPDATE_STOP_LOSS",
                                      symbol="NIFTY", quantity_percent=None,
                                      quantity_basis="NONE", stop_loss=Decimal("24200")))
        pos = pos_repo.get_position("default", "NIFTY", "LONG")
        assert pos.current_allocation_pct == Decimal("100")  # unchanged
        assert pos.stop_loss == Decimal("24200")


# ---------------------------------------------------------------------------
# Test: Missing position → manual review
# ---------------------------------------------------------------------------
class TestMissingPosition:
    def test_reduce_missing_position_goes_to_review(self):
        engine, pos_repo, review_repo = make_engine()

        result = engine.apply_event(make_event(source_message_id="reduce-001",
                                               final_action="REDUCE_POSITION",
                                               symbol="UNKNOWNSYM",
                                               quantity_percent=Decimal("50"),
                                               quantity_basis="CURRENT_HOLDING"))
        assert result.status == "MANUAL_REVIEW"
        assert len(review_repo.list_pending()) == 1


# ---------------------------------------------------------------------------
# Test: Duplicate processing
# ---------------------------------------------------------------------------
class TestDuplicateProcessing:
    def test_same_message_id_twice(self):
        engine, pos_repo, _ = make_engine()

        r1 = engine.apply_event(make_event(source_message_id="dup-001", final_action="OPEN_LONG",
                                           symbol="COPPER"))
        assert r1.status == "OK"

        r2 = engine.apply_event(make_event(source_message_id="dup-001", final_action="OPEN_LONG",
                                           symbol="COPPER"))
        assert r2.status == "ALREADY_PROCESSED"

        # Only one open position
        positions = [p for p in pos_repo.list_open_positions() if p.symbol == "COPPER"]
        assert len(positions) == 1


# ---------------------------------------------------------------------------
# Test: Sell with existing long → reduce
# ---------------------------------------------------------------------------
class TestSellWithExistingLong:
    def test_existing_long_standalone_sell_is_blocked(self):
        """
        Old invalid assumption: standalone SELL reduced an existing LONG.
        Confirmed v2 behavior: it is an opposite-direction conflict.
        """
        engine, pos_repo, review_repo = make_engine()

        # Open 100%
        engine.apply_event(make_event(source_message_id="open-001", final_action="OPEN_LONG",
                                      symbol="NIFTY", quantity_percent=Decimal("100")))

        result = engine.apply_event(make_event(source_message_id="sell-001", final_action="OPEN_SHORT",
                                               symbol="NIFTY", direction="SHORT",
                                               quantity_percent=Decimal("50"),
                                               quantity_basis="CUSTOMER_BUYING_CAPACITY"))

        pos = pos_repo.get_position("default", "NIFTY", "LONG")
        assert result.status == "MANUAL_REVIEW"
        assert pos.current_allocation_pct == Decimal("100")
        assert len(review_repo.list_pending()) == 1


# ---------------------------------------------------------------------------
# Test: No existing long → open short
# ---------------------------------------------------------------------------
class TestOpenShortWhenNoLong:
    def test_open_short(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="short-001",
                                      final_action="OPEN_SHORT",
                                      symbol="COPPER",
                                      direction="SHORT",
                                      quantity_percent=Decimal("100"),
                                      quantity_basis="MODEL_ALLOCATION",
                                      execution_price_primary=Decimal("6.43"),
                                      stop_loss=Decimal("6.51")))
        pos = pos_repo.get_position("default", "COPPER", "SHORT")
        assert pos is not None
        assert pos.status == "OPEN"
        assert pos.direction == "SHORT"


# ---------------------------------------------------------------------------
# Test: Opposite direction → manual review
# ---------------------------------------------------------------------------
class TestOppositeDirection:
    def test_existing_long_new_short_goes_to_review(self):
        engine, pos_repo, review_repo = make_engine()

        # Open long
        engine.apply_event(make_event(source_message_id="long-001",
                                      final_action="OPEN_LONG", symbol="NVDA"))

        # Try to open short (opposite direction conflict)
        result = engine.apply_event(make_event(source_message_id="short-001",
                                               final_action="OPEN_SHORT", symbol="NVDA",
                                               direction="SHORT"))
        assert result.status == "MANUAL_REVIEW"

    def test_existing_short_new_long_goes_to_review(self):
        engine, pos_repo, review_repo = make_engine()

        engine.apply_event(make_event(source_message_id="short-setup",
                                      final_action="OPEN_SHORT", symbol="TSLA",
                                      direction="SHORT"))

        result = engine.apply_event(make_event(source_message_id="long-conflict",
                                               final_action="OPEN_LONG", symbol="TSLA",
                                               direction="LONG"))

        short_pos = pos_repo.get_position("default", "TSLA", "SHORT")
        assert result.status == "MANUAL_REVIEW"
        assert short_pos.current_allocation_pct == Decimal("100")
        assert len(review_repo.list_pending()) == 1


# ---------------------------------------------------------------------------
# Test: Target hit (no position change)
# ---------------------------------------------------------------------------
class TestTargetHit:
    def test_target_hit_no_change(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="open-001", final_action="OPEN_LONG",
                                      symbol="SNDK", quantity_percent=Decimal("100")))

        engine.apply_event(make_event(source_message_id="tgt-001", final_action="TARGET_HIT",
                                      symbol="SNDK", quantity_percent=None,
                                      quantity_basis="NONE"))

        pos = pos_repo.get_position("default", "SNDK", "LONG")
        assert pos.current_allocation_pct == Decimal("100")  # unchanged


# ---------------------------------------------------------------------------
# Test: Weighted average entry price on add
# ---------------------------------------------------------------------------
class TestWeightedAverageEntry:
    def test_weighted_avg_price(self):
        engine, pos_repo, _ = make_engine()

        engine.apply_event(make_event(source_message_id="open-001", final_action="OPEN_LONG",
                                      symbol="MU", quantity_percent=Decimal("50"),
                                      execution_price_primary=Decimal("100")))

        engine.apply_event(make_event(source_message_id="add-001", final_action="ADD_LONG",
                                      symbol="MU", direction="LONG",
                                      quantity_percent=Decimal("50"),
                                      execution_price_primary=Decimal("120")))

        pos = pos_repo.get_position("default", "MU", "LONG")
        assert pos.current_allocation_pct == Decimal("100")
        # Weighted avg: (50*100 + 50*120) / 100 = 110
        assert pos.average_entry_price == Decimal("110")
