"""
test_rule_parser.py
===================
Tests for src/rule_parser.py — all R01-R13 rules and sell ambiguity.
"""
import pytest
from decimal import Decimal

from src.entity_extractor import EntityExtractor, ExtractionResult
from src.rule_parser import apply_rules


def _extract(text: str) -> ExtractionResult:
    return EntityExtractor().extract(text)


class TestR01_ProfitBook:
    def test_50_pct_profit_book(self):
        text = "50% PROFIT BOOK IN NIFTY @25942"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "REDUCE_POSITION"
        assert r.quantity_percent == Decimal("50")
        assert r.quantity_basis == "CURRENT_HOLDING"

    def test_remaining_multiplier(self):
        text = "50% PROFIT BOOK IN NIFTY"
        r = apply_rules(text, _extract(text))
        assert r.remaining_holding_multiplier == Decimal("0.5")

    def test_25_pct_profit_book(self):
        text = "Book 25% profit in NVDA"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "REDUCE_POSITION"
        assert r.quantity_percent == Decimal("25")

    def test_literal_reduce_with_percentage(self):
        text = "REDUCE 50% IN RELIANCE"
        r = apply_rules(text, _extract(text), existing_long=True, has_holdings_context=True)
        assert r.rule_action == "REDUCE_POSITION"
        assert r.quantity_percent == Decimal("50")
        assert r.quantity_basis == "CURRENT_HOLDING"
        assert r.remaining_holding_multiplier == Decimal("0.5")


class TestR02_PartProfit:
    def test_part_profit(self):
        text = "Part profit in INFY"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "REDUCE_POSITION"
        assert r.quantity_percent == Decimal("25")
        assert r.remaining_holding_multiplier == Decimal("0.75")

    def test_partial_profit(self):
        text = "Partial profit book"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "REDUCE_POSITION"
        assert r.quantity_percent == Decimal("25")


class TestR03_FullClose:
    def test_full_profit(self):
        text = "Full profit book in AMD"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "CLOSE_POSITION"
        assert r.quantity_percent == Decimal("100")
        assert r.remaining_holding_multiplier == Decimal("0")

    def test_book_profit(self):
        text = "Book profit in INFY"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "CLOSE_POSITION"

    def test_exit(self):
        text = "Exit NIFTY"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "CLOSE_POSITION"

    def test_sl_touch(self):
        text = "SL TOUCH IN JSWSTEEL"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "CLOSE_POSITION"
        assert r.rule_confidence == 1.0

    def test_sl_touched(self):
        text = "SL touched in Gold"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "CLOSE_POSITION"


class TestR04_Buy:
    def test_buy_with_pct(self):
        text = "Buy 50% NVDA @145"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "OPEN_LONG"
        assert r.direction == "LONG"

    def test_buy_without_pct(self):
        text = "BUY INFY @1800 SL 1750 TGT 1900"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "OPEN_LONG"
        assert r.direction == "LONG"


class TestR07_Hold:
    def test_hold_only(self):
        text = "Hold NVDA"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "HOLD_POSITION"

    def test_hold_with_sl(self):
        text = "Hold NVDA with SL 145"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "UPDATE_STOP_LOSS"

    def test_positional_sl(self):
        text = "Positional SL for Nifty: 23900"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "UPDATE_STOP_LOSS"


class TestR09_TargetHit:
    def test_target_touched(self):
        text = "Target touched in SNDK"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "TARGET_HIT"
        assert r.rule_confidence == 1.0

    def test_first_target_touched(self):
        text = "1st target touched in NIFTY"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "TARGET_HIT"


class TestR10_Ignore:
    def test_ignore(self):
        text = "Ignore the previous NG message"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "CANCEL_PREVIOUS"
        assert r.requires_context


class TestR11_Correction:
    def test_correction(self):
        text = "CORRECTION: 50% BUY GOLD @156950"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "CORRECTION"
        assert r.is_correction
        assert r.requires_context
        assert r.needs_review


class TestR13_Conditional:
    def test_if_conditional(self):
        text = "If Dow crosses 45000, buy"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "CONDITIONAL_INSTRUCTION"
        assert r.is_conditional
        assert r.requires_context

    def test_when_triggered(self):
        text = "Add long position in Nasdaq @ 30300 (when triggered)"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "CONDITIONAL_INSTRUCTION"


class TestAddLong:
    def test_again_buy(self):
        text = "AGAIN BUY NIFTY @23145 SL 23000"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "ADD_LONG"

    def test_add_long(self):
        text = "Add long position in NVDA @150"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "ADD_LONG"


class TestAddShort:
    def test_again_sell(self):
        text = "Again sell COPPER @1350"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "ADD_SHORT"

    def test_add_short(self):
        text = "Add short position in crude oil at 93.50"
        r = apply_rules(text, _extract(text))
        assert r.rule_action == "ADD_SHORT"


class TestSellAmbiguity:
    def test_rule_a_existing_long(self):
        """Rule A: existing long + sell → reduce."""
        text = "Sell 50% Nifty"
        r = apply_rules(text, _extract(text), existing_long=True)
        assert r.rule_action == "REDUCE_POSITION"
        assert r.quantity_percent == Decimal("50")

    def test_rule_c_no_long_short_cues(self):
        """Rule C: no long, clear short cues → open short."""
        text = "Sell copper @6.43 with sl 6.51 for target 6.30"
        r = apply_rules(text, _extract(text), existing_long=False, has_holdings_context=True)
        assert r.rule_action == "OPEN_SHORT"

    def test_rule_d_ambiguous(self):
        """Rule D: no context → AMBIGUOUS."""
        text = "Sell 50% XYZ"
        r = apply_rules(text, _extract(text), has_holdings_context=False)
        assert r.rule_action == "AMBIGUOUS"
        assert r.needs_review
