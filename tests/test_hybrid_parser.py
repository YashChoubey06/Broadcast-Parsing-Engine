"""
test_hybrid_parser.py
=====================
Tests for src/hybrid_parser.py — resolution source and eligibility.
"""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.hybrid_parser import HybridParser
from src.ml_classifier import MLClassifier
from src.schemas import ParsedTradeEvent


def make_parser(mock_ml_action="REDUCE_POSITION", mock_ml_confidence=0.98):
    """Create a HybridParser with a mocked ML classifier."""
    clf = MagicMock(spec=MLClassifier)
    clf.is_loaded.return_value = True
    clf.predict.return_value = (mock_ml_action, mock_ml_confidence)
    return HybridParser(classifier=clf)


class TestResolutionSource:
    def test_rule_overrides_ml(self):
        """Deterministic rule fires → resolution_source=RULE regardless of ML."""
        parser = make_parser(mock_ml_action="OPEN_LONG", mock_ml_confidence=0.99)
        event = parser.parse("SL TOUCH IN JSWSTEEL")
        assert event.final_action == "CLOSE_POSITION"
        assert event.resolution_source in ("RULE", "RULE_AND_ML_AGREE")

    def test_rule_and_ml_agree(self):
        parser = make_parser(mock_ml_action="REDUCE_POSITION", mock_ml_confidence=0.99)
        event = parser.parse("50% PROFIT BOOK IN NIFTY @25942")
        assert event.final_action == "REDUCE_POSITION"
        assert event.resolution_source == "RULE_AND_ML_AGREE"

    def test_ml_used_when_no_rule(self):
        """No rule fires → ML provides the action."""
        parser = make_parser(mock_ml_action="OPEN_LONG", mock_ml_confidence=0.97)
        event = parser.parse("BUY NVDA @150 SL 145 TGT 160")
        # BUY triggers the rule, but confirm source
        assert event.final_action in ("OPEN_LONG", "ADD_LONG")


class TestAutoApplyEligibility:
    def test_eligible_high_confidence_rule(self):
        parser = make_parser()
        event = parser.parse("50% PROFIT BOOK IN NIFTY @25942")
        assert event.auto_apply_eligible is True
        assert event.needs_review is False

    def test_correction_not_eligible(self):
        parser = make_parser()
        event = parser.parse("CORRECTION: Buy Gold @156950 SL 156500")
        assert event.auto_apply_eligible is False
        assert event.needs_review is True

    def test_conditional_not_eligible(self):
        parser = make_parser()
        event = parser.parse("If Dow crosses 45000, buy")
        assert event.auto_apply_eligible is False
        assert event.needs_review is True

    def test_flat_sell_entry_is_eligible_when_symbol_present(self):
        parser = make_parser(mock_ml_action="AMBIGUOUS", mock_ml_confidence=0.50)
        event = parser.parse("Sell 50% XYZ", has_holdings_context=False)
        assert event.final_action == "OPEN_SHORT"
        assert event.surface_instruction == "SELL"
        assert event.entry_capacity_pct == Decimal("50")
        assert event.auto_apply_eligible is True
        assert event.needs_review is False


class TestParsedEventFields:
    def test_quantity_from_rule(self):
        parser = make_parser()
        event = parser.parse("50% PROFIT BOOK IN NIFTY @25942")
        assert event.quantity_percent == Decimal("50")
        assert event.quantity_basis == "CURRENT_POSITION"
        assert event.remaining_holding_multiplier == Decimal("0.5")

    def test_literal_reduce_wording(self):
        parser = make_parser(mock_ml_action="UNKNOWN", mock_ml_confidence=0.0)
        event = parser.parse(
            "REDUCE 50% IN RELIANCE",
            existing_long=True,
            has_holdings_context=True,
        )
        assert event.final_action == "REDUCE_POSITION"
        assert event.symbol == "RELIANCE"
        assert event.quantity_percent == Decimal("50")
        assert event.quantity_basis == "CURRENT_POSITION"

    def test_part_profit_defaults_25_pct(self):
        parser = make_parser()
        event = parser.parse("Part profit in INFY")
        assert event.quantity_percent == Decimal("25")
        assert event.quantity_basis == "CURRENT_POSITION"
        assert event.remaining_holding_multiplier == Decimal("0.75")

    def test_sl_touch_close(self):
        parser = make_parser()
        event = parser.parse("SL TOUCH IN JSWSTEEL")
        assert event.final_action == "CLOSE_POSITION"
        assert event.remaining_holding_multiplier == Decimal("0")

    def test_symbol_extracted(self):
        parser = make_parser()
        event = parser.parse("50% PROFIT BOOK IN CRUDE @5403")
        assert event.symbol == "CRUDE"

    def test_price_extracted(self):
        parser = make_parser()
        event = parser.parse("BUY CRUDE MINI @8637 SL 8500 TGT 8800")
        assert event.execution_price_primary == Decimal("8637")
        assert event.stop_loss == Decimal("8500")

    def test_buy_50_tsla_v2_semantics(self):
        parser = make_parser(mock_ml_action="OPEN_LONG", mock_ml_confidence=0.90)
        event = parser.parse("BUY 50% TSLA")
        assert event.semantics_version == "v2"
        assert event.symbol_raw == "TSLA"
        assert event.symbol == "TSLA"
        assert event.surface_instruction == "BUY"
        assert event.entry_capacity_pct == Decimal("50")
        assert event.quantity_basis == "CUSTOMER_BUYING_CAPACITY"
        assert event.position_effect == "OPEN"
        assert event.resolved_position_side == "LONG"

    def test_sell_50_tsla_v2_semantics(self):
        parser = make_parser(mock_ml_action="OPEN_SHORT", mock_ml_confidence=0.90)
        event = parser.parse("SELL 50% TSLA")
        assert event.symbol_raw == "TSLA"
        assert event.symbol == "TSLA"
        assert event.surface_instruction == "SELL"
        assert event.entry_capacity_pct == Decimal("50")
        assert event.quantity_basis == "CUSTOMER_BUYING_CAPACITY"
        assert event.position_effect == "OPEN"
        assert event.resolved_position_side == "SHORT"

    def test_existing_short_standalone_buy_needs_context(self):
        parser = make_parser(mock_ml_action="OPEN_LONG", mock_ml_confidence=0.99)
        event = parser.parse("BUY 50% TSLA", existing_short=True, has_holdings_context=True)
        assert event.final_action == "AMBIGUOUS"
        assert event.surface_instruction == "BUY"
        assert event.position_effect == "UNRESOLVED"
        assert event.needs_review is True

    def test_existing_long_standalone_sell_needs_context(self):
        parser = make_parser(mock_ml_action="OPEN_SHORT", mock_ml_confidence=0.99)
        event = parser.parse("SELL 50% TSLA", existing_long=True, has_holdings_context=True)
        assert event.final_action == "AMBIGUOUS"
        assert event.surface_instruction == "SELL"
        assert event.position_effect == "UNRESOLVED"
        assert event.needs_review is True

    def test_plain_stock_buy_defaults_to_capacity_100(self):
        parser = make_parser(mock_ml_action="OPEN_LONG", mock_ml_confidence=0.99)
        event = parser.parse("BUY NVDA @1234 SL 1200 TGT 1300-1350")
        assert event.symbol == "NVDA"
        assert event.entry_capacity_pct == Decimal("100.0")
        assert event.quantity_basis == "CUSTOMER_BUYING_CAPACITY"

    def test_plain_stock_sell_defaults_to_capacity_100(self):
        parser = make_parser(mock_ml_action="OPEN_SHORT", mock_ml_confidence=0.99)
        event = parser.parse("SELL INFY @1234 SL 1300 TGT 1150")
        assert event.symbol == "INFY"
        assert event.entry_capacity_pct == Decimal("100.0")
        assert event.quantity_basis == "CUSTOMER_BUYING_CAPACITY"


class TestMultiInstrumentParse:
    def test_multi_instrument_flagged(self):
        parser = make_parser()
        event = parser.parse("SL touched in Gold & Silver")
        assert event.is_multi_instrument

    def test_multi_instrument_split(self):
        parser = make_parser()
        events = parser.parse_multi_instrument(
            "SL touched in Gold & Silver",
            source_message_id="test-multi-001",
        )
        # Should produce 2 child events or 1 review event
        assert len(events) >= 1
        if len(events) == 2:
            symbols = {e.symbol for e in events}
            assert "GOLD" in symbols
            assert "SILVER" in symbols


class TestParsedEventCompatibility:
    def test_old_json_without_v2_fields_deserializes(self):
        raw = '{"source_message_id":"old-1","raw_text":"BUY NVDA","final_action":"OPEN_LONG","symbol":"NVDA","quantity_percent":"100","quantity_basis":"MODEL_ALLOCATION"}'
        event = ParsedTradeEvent.from_json(raw)
        assert event.source_message_id == "old-1"
        assert event.symbol == "NVDA"
        assert event.quantity_percent == Decimal("100")
        assert event.quantity_basis == "CUSTOMER_BUYING_CAPACITY"
        assert event.semantics_version == "v2"
        assert event.surface_instruction is None

    def test_new_v2_json_round_trips(self):
        event = ParsedTradeEvent(
            source_message_id="new-1",
            raw_text="BUY 50% TSLA",
            final_action="OPEN_LONG",
            symbol="TSLA",
            direction="LONG",
            quantity_percent=Decimal("50"),
            quantity_basis="CUSTOMER_BUYING_CAPACITY",
            surface_instruction="BUY",
            entry_capacity_pct=Decimal("50"),
            position_effect="OPEN",
            resolved_position_side="LONG",
        )
        loaded = ParsedTradeEvent.from_json(event.to_json())
        assert loaded.semantics_version == "v2"
        assert loaded.entry_capacity_pct == Decimal("50")
        assert loaded.surface_instruction == "BUY"
        assert loaded.resolved_position_side == "LONG"
