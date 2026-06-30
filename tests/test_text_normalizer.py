"""
test_text_normalizer.py
=======================
Tests for src/text_normalizer.py
"""
import pytest
from src.text_normalizer import normalize, normalize_for_matching, normalize_for_ml


class TestNormalize:
    def test_strips_leading_trailing_whitespace(self):
        assert normalize("  hello  ") == "hello"

    def test_collapses_internal_spaces(self):
        assert normalize("BUY   NIFTY  @  25000") == "BUY NIFTY @ 25000"

    def test_replaces_escaped_newline(self):
        assert normalize("line1\\nline2") == "line1 line2"

    def test_replaces_literal_newline(self):
        assert normalize("line1\nline2") == "line1 line2"

    def test_replaces_carriage_return(self):
        assert normalize("line1\r\nline2") == "line1 line2"

    def test_normalises_em_dash(self):
        result = normalize("8800\u20149100")  # em-dash
        assert result == "8800-9100"

    def test_normalises_en_dash(self):
        result = normalize("8800\u20139100")  # en-dash
        assert result == "8800-9100"

    def test_preserves_percent(self):
        assert "50%" in normalize("50% PROFIT BOOK")

    def test_preserves_at_sign(self):
        assert "@" in normalize("BUY NIFTY @25000")

    def test_preserves_numeric_values(self):
        result = normalize("SL 26200 TGT 25800")
        assert "26200" in result
        assert "25800" in result

    def test_preserves_slash(self):
        assert "/" in normalize("S/L 26200")

    def test_preserves_dot(self):
        assert "." in normalize("@6.43")

    def test_preserves_ampersand(self):
        assert "&" in normalize("Gold & Silver")

    def test_empty_string(self):
        assert normalize("") == ""

    def test_none_safe(self):
        assert normalize(None) == ""

    def test_normalize_for_matching_uppercases(self):
        result = normalize_for_matching("buy nifty @25000")
        assert result == result.upper()

    def test_normalize_for_ml_lowercases(self):
        result = normalize_for_ml("BUY NIFTY @25000")
        assert result == result.lower()
