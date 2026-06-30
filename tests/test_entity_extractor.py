"""
test_entity_extractor.py
========================
Tests for src/entity_extractor.py
"""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock

from src.entity_extractor import EntityExtractor
from src.alias_repository import AliasRepository


def make_extractor():
    """Create an extractor with the real alias repo."""
    return EntityExtractor()


class TestSymbolExtraction:
    def test_single_symbol(self):
        ex = make_extractor()
        er = ex.extract("50% PROFIT BOOK IN NIFTY @25942")
        assert er.symbol == "NIFTY"
        assert er.symbols == ["NIFTY"]
        assert not er.is_multi_instrument

    def test_crude_mini_alias(self):
        ex = make_extractor()
        er = ex.extract("BUY CRUDE MINI @8637 SL 8500")
        assert er.symbol == "CRUDE_MINI"

    def test_crudeoilm_alias(self):
        ex = make_extractor()
        er = ex.extract("CRUDEOILM profit book")
        assert er.symbol == "CRUDE_MINI"

    def test_ng_to_natural_gas(self):
        ex = make_extractor()
        er = ex.extract("NG SL touch")
        assert er.symbol == "NATURAL_GAS"

    def test_multi_instrument_detected(self):
        ex = make_extractor()
        er = ex.extract("SL touched in Gold & Silver")
        assert er.is_multi_instrument
        assert "GOLD" in er.symbols
        assert "SILVER" in er.symbols

    def test_no_symbol_found(self):
        ex = make_extractor()
        er = ex.extract("This is a random admin message")
        assert er.symbol is None
        assert er.symbols == []


class TestPercentageExtraction:
    def test_explicit_percentage(self):
        ex = make_extractor()
        er = ex.extract("50% PROFIT BOOK IN NIFTY")
        assert er.quantity_percent == Decimal("50")

    def test_percentage_with_decimal(self):
        ex = make_extractor()
        er = ex.extract("25.5% profit book in CRUDE")
        assert er.quantity_percent == Decimal("25.5")

    def test_no_percentage(self):
        ex = make_extractor()
        er = ex.extract("BUY NVDA @145 SL 140")
        assert er.quantity_percent is None


class TestPriceExtraction:
    def test_entry_price_at(self):
        ex = make_extractor()
        er = ex.extract("BUY NIFTY @25942 SL 26200")
        assert er.execution_price_primary == Decimal("25942")

    def test_entry_price_at_decimal(self):
        ex = make_extractor()
        er = ex.extract("Sell copper @6.43 with sl 6.51")
        assert er.execution_price_primary == Decimal("6.43")

    def test_stop_loss(self):
        ex = make_extractor()
        er = ex.extract("BUY NIFTY @25942 SL 26200")
        assert er.stop_loss == Decimal("26200")

    def test_stop_loss_slash(self):
        ex = make_extractor()
        er = ex.extract("Add short crude oil at 93.50 with S/L 96.00")
        assert er.stop_loss == Decimal("96.00")

    def test_targets_single(self):
        ex = make_extractor()
        er = ex.extract("BUY NIFTY @25000 TGT 25800")
        assert Decimal("25800") in er.targets

    def test_targets_range(self):
        ex = make_extractor()
        er = ex.extract("BUY CRUDE @8637 TGT 8800-9100")
        assert Decimal("8800") in er.targets
        assert Decimal("9100") in er.targets


class TestFlags:
    def test_correction_flag(self):
        ex = make_extractor()
        er = ex.extract("CORRECTION: Buy Gold @156950")
        assert er.is_correction

    def test_conditional_flag(self):
        ex = make_extractor()
        er = ex.extract("If Dow crosses 45000, buy")
        assert er.is_conditional

    def test_part_profit(self):
        ex = make_extractor()
        er = ex.extract("Part profit in INFY")
        assert er.has_part_profit

    def test_full_profit(self):
        ex = make_extractor()
        er = ex.extract("Book full profit in AMD")
        assert er.has_full_profit

    def test_sl_touch(self):
        ex = make_extractor()
        er = ex.extract("SL TOUCH IN JSWSTEEL")
        assert er.has_sl_touch

    def test_hold(self):
        ex = make_extractor()
        er = ex.extract("Hold NVDA with SL 145")
        assert er.has_hold

    def test_ignore(self):
        ex = make_extractor()
        er = ex.extract("Ignore the previous NG message")
        assert er.has_ignore


class TestContractMonth:
    def test_extracts_month(self):
        ex = make_extractor()
        er = ex.extract("Sell 50% gold Jun at above price.")
        assert er.contract_month == "JUN"

    def test_extracts_aug(self):
        ex = make_extractor()
        er = ex.extract("BUY CRUDE AUG @5400")
        assert er.contract_month == "AUG"
