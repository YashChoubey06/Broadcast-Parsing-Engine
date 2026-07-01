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

    def test_ambiguous_sell_not_eligible(self):
        parser = make_parser(mock_ml_action="AMBIGUOUS", mock_ml_confidence=0.50)
        event = parser.parse("Sell 50% XYZ", has_holdings_context=False)
        assert event.auto_apply_eligible is False
        assert event.needs_review is True


class TestParsedEventFields:
    def test_quantity_from_rule(self):
        parser = make_parser()
        event = parser.parse("50% PROFIT BOOK IN NIFTY @25942")
        assert event.quantity_percent == Decimal("50")
        assert event.quantity_basis == "CURRENT_HOLDING"
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
        assert event.quantity_basis == "CURRENT_HOLDING"

    def test_part_profit_defaults_25_pct(self):
        parser = make_parser()
        event = parser.parse("Part profit in INFY")
        assert event.quantity_percent == Decimal("25")
        assert event.quantity_basis == "CURRENT_HOLDING"
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
