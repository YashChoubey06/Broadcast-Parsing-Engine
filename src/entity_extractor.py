"""
entity_extractor.py
===================
Layer 3 of the hybrid parser.

Extracts structured trade fields from normalised text using regular expressions
and the instrument alias dictionary.

Fields extracted:
  - symbol_raw, symbol, symbols, is_multi_instrument
  - quantity_percent
  - execution_prices (list), execution_price_primary
  - stop_loss
  - targets (list)
  - contract_month
  - strike_price, option_type
  - direction hint
  - is_correction, is_conditional
  - has_part_profit, has_full_profit
  - has_sl_touch
  - has_hold, has_update_sl

IMPORTANT: This module does NOT decide the final action.
           It only extracts entities and flags for the rule parser and
           hybrid resolver to use.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from src.text_normalizer import normalize
from src.alias_repository import AliasRepository, get_alias_repository


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# A price / numeric value (handles integers, decimals, and comma-thousands)
_NUM = r"[\d,]+(?:\.\d+)?"

# Percentage: "50%"  "50 %"
_PCT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

# Entry price: "@1234" or "at 1234" or "@ 1234.5"
_ENTRY_RE = re.compile(
    r"(?:@|at\s)[\s]*(" + _NUM + r")",
    re.IGNORECASE,
)

# Stop loss: "sl 1234", "s/l 1234", "stoploss 1234", "stop 1234", "stop loss 1234"
_SL_RE = re.compile(
    r"(?:s/?l|stoploss|stop\s+loss|stop)[:\s]+(" + _NUM + r")",
    re.IGNORECASE,
)

# Targets: "tgt 1234-5678", "target 1234", "t1 1234", "1st target 1234"
_TGT_RE = re.compile(
    r"(?:tgt|target|t1|t2|1st\s+target|2nd\s+target)[:\s]+(" + _NUM + r"(?:\s*[-&/]\s*" + _NUM + r")*)",
    re.IGNORECASE,
)

# Contract month (JAN-DEC, or JULY/AUG etc.)
_MONTHS = (
    "JAN|FEB|MAR|APR|MAY|JUN|JUL|JULY|AUG|SEP|OCT|NOV|DEC|"
    "JANUARY|FEBRUARY|MARCH|APRIL|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER"
)
_CONTRACT_MONTH_RE = re.compile(
    r"\b(" + _MONTHS + r")\b",
    re.IGNORECASE,
)

# Month normalisation map
_MONTH_NORM = {
    "JANUARY": "JAN", "FEBRUARY": "FEB", "MARCH": "MAR",
    "APRIL": "APR", "MAY": "MAY", "JUNE": "JUN", "JULY": "JUL",
    "AUGUST": "AUG", "SEPTEMBER": "SEP", "OCTOBER": "OCT",
    "NOVEMBER": "NOV", "DECEMBER": "DEC",
    "JAN": "JAN", "FEB": "FEB", "MAR": "MAR", "APR": "APR",
    "JUN": "JUN", "JUL": "JUL", "AUG": "AUG", "SEP": "SEP",
    "OCT": "OCT", "NOV": "NOV", "DEC": "DEC",
}

# Option type
_OPTION_TYPE_RE = re.compile(
    r"\b(CE|PE|CALL|PUT)\b",
    re.IGNORECASE,
)

# Strike price: "25000 CE" or "CE 25000"
_STRIKE_RE = re.compile(
    r"(\d{4,6})\s*(?:CE|PE|CALL|PUT)|(?:CE|PE|CALL|PUT)\s*(\d{4,6})",
    re.IGNORECASE,
)

# Direction hints in text
_LONG_RE = re.compile(r"\b(BUY|LONG|ADD\s+LONG|AGAIN\s+BUY|RE-?BUY)\b", re.IGNORECASE)
_SHORT_RE = re.compile(r"\b(SELL|SHORT|ADD\s+SHORT|AGAIN\s+SELL)\b", re.IGNORECASE)
_ENTRY_SYMBOL_FALLBACK_RE = re.compile(
    r"\b(?:BUY|SELL)\s+\d+(?:\.\d+)?\s*%\s+([A-Z][A-Z0-9._-]{1,14})\b",
    re.IGNORECASE,
)
_PCT_ENTRY_SYMBOL_FALLBACK_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*%\s+(?:BUY|SELL)\s+([A-Z][A-Z0-9._-]{1,14})\b",
    re.IGNORECASE,
)
_IN_SYMBOL_FALLBACK_RE = re.compile(
    r"\b(?:IN|FROM)\s+([A-Z][A-Z0-9._-]{1,14})\b",
    re.IGNORECASE,
)
_SYMBOL_FALLBACK_STOPWORDS = {
    "AT", "CMP", "ABOVE", "BELOW", "IF", "WHEN", "WITH", "SL", "STOP",
    "TGT", "TARGET", "PROFIT", "BOOK", "FULL", "PART", "EXIT", "FROM",
    "POSITION", "POSITIONS", "LONG", "SHORT",
}

# Correction flag
_CORRECTION_RE = re.compile(r"\bCORRECTION\b", re.IGNORECASE)

# Conditional flag
_CONDITIONAL_RE = re.compile(
    r"\b(IF\s+|WHEN\s+TRIGGERED|ABOVE\s+THIS\s+LEVEL|BELOW\s+THIS\s+LEVEL|WHEN\s+CROSSED)\b",
    re.IGNORECASE,
)

# Part/partial profit
_PART_PROFIT_RE = re.compile(r"\b(PART\s+PROFIT|PARTIAL\s+PROFIT)\b", re.IGNORECASE)

# Full profit / book profit / exit keywords
_FULL_PROFIT_RE = re.compile(
    r"\b(FULL\s+PROFIT|BOOK\s+FULL\s+PROFIT|BOOK\s+PROFIT|FULL\s+PROFIT\s+BOOK|EXIT\s+FROM|EXIT)\b",
    re.IGNORECASE,
)

# SL touch keywords
_SL_TOUCH_RE = re.compile(
    r"\b(SL\s+TOUCH(?:ED)?|STOP\s+LOSS\s+HIT|SL\s+HIT)\b",
    re.IGNORECASE,
)

# Target hit keywords
_TARGET_TOUCH_RE = re.compile(
    r"\b(?:\d+(?:st|nd|rd|th)\s+)?TARGET\s+(?:TOUCH(?:ED)?|HIT)\b",
    re.IGNORECASE,
)

# Hold keywords
_HOLD_RE = re.compile(r"\bHOLD\b", re.IGNORECASE)

# Ignore / cancel keywords
_IGNORE_RE = re.compile(r"\bIGNORE\b", re.IGNORECASE)

# Group / multi-position phrases
_GROUP_RE = re.compile(
    r"\b(ALL\s+POSITIONS|LONG\s+POSITIONS|SHORT\s+POSITIONS|INDICES)\b",
    re.IGNORECASE,
)

# Multi-instrument separators (&, AND, ,)
_MULTI_SEP_RE = re.compile(r"\s+AND\s+|\s*&\s*|\s*,\s*(?=[A-Z])", re.IGNORECASE)


def _clean_num(s: str) -> Optional[Decimal]:
    """Strip commas and convert to Decimal, or return None on failure."""
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None


def _parse_price_list(raw: str) -> list[Decimal]:
    """
    Parse a string like "25800" or "8800-9100" or "8800 9100" into a list
    of Decimal values.
    """
    # Split on hyphen (range) or slash or space-separated values
    parts = re.split(r"[-/\s]+", raw.strip())
    result = []
    for p in parts:
        v = _clean_num(p.strip())
        if v is not None:
            result.append(v)
    return result


# ---------------------------------------------------------------------------
# ExtractionResult
# ---------------------------------------------------------------------------
class ExtractionResult:
    """
    Container for all entities extracted from a single message.
    """

    __slots__ = (
        "symbol_raw", "symbol", "symbols",
        "is_multi_instrument",
        "quantity_percent",
        "execution_prices", "execution_price_primary",
        "stop_loss", "targets",
        "contract_month",
        "strike_price", "option_type",
        "direction_hint",
        "is_correction", "is_conditional",
        "has_part_profit", "has_full_profit",
        "has_sl_touch", "has_target_touch",
        "has_hold", "has_ignore", "has_group_action",
    )

    def __init__(self):
        self.symbol_raw: Optional[str] = None
        self.symbol: Optional[str] = None
        self.symbols: list[str] = []
        self.is_multi_instrument: bool = False
        self.quantity_percent: Optional[Decimal] = None
        self.execution_prices: list[Decimal] = []
        self.execution_price_primary: Optional[Decimal] = None
        self.stop_loss: Optional[Decimal] = None
        self.targets: list[Decimal] = []
        self.contract_month: Optional[str] = None
        self.strike_price: Optional[Decimal] = None
        self.option_type: Optional[str] = None
        self.direction_hint: Optional[str] = None  # LONG | SHORT
        self.is_correction: bool = False
        self.is_conditional: bool = False
        self.has_part_profit: bool = False
        self.has_full_profit: bool = False
        self.has_sl_touch: bool = False
        self.has_target_touch: bool = False
        self.has_hold: bool = False
        self.has_ignore: bool = False
        self.has_group_action: bool = False


class EntityExtractor:
    """
    Extracts all trade entities from a normalised message string using
    deterministic regex patterns and alias lookups.
    """

    def __init__(self, alias_repo: Optional[AliasRepository] = None) -> None:
        self._aliases = alias_repo or get_alias_repository()

    def extract(self, text: str) -> ExtractionResult:
        """
        Run all extractors and return an ExtractionResult.

        *text* should already be normalised (but case is preserved here;
        individual patterns use re.IGNORECASE).
        """
        er = ExtractionResult()
        upper = text.upper()

        # ---- Flags ---------------------------------------------------------
        er.is_correction = bool(_CORRECTION_RE.search(text))
        er.is_conditional = bool(_CONDITIONAL_RE.search(text))
        er.has_part_profit = bool(_PART_PROFIT_RE.search(text))
        er.has_full_profit = bool(_FULL_PROFIT_RE.search(text))
        er.has_sl_touch = bool(_SL_TOUCH_RE.search(text))
        er.has_target_touch = bool(_TARGET_TOUCH_RE.search(text))
        er.has_hold = bool(_HOLD_RE.search(text))
        er.has_ignore = bool(_IGNORE_RE.search(text))
        er.has_group_action = bool(_GROUP_RE.search(text))

        # ---- Direction hint ------------------------------------------------
        if _LONG_RE.search(text):
            er.direction_hint = "LONG"
        if _SHORT_RE.search(text):
            # SHORT overrides if SL/target pattern suggests short entry
            er.direction_hint = "SHORT"

        # ---- Symbols -------------------------------------------------------
        matches = self._aliases.find_in_text(text)
        if matches:
            canonicals = list(dict.fromkeys(m[1] for m in matches))  # preserve order, dedupe
            er.symbols = canonicals
            if len(canonicals) == 1:
                er.symbol_raw = matches[0][0]
                er.symbol = canonicals[0]
            else:
                er.is_multi_instrument = True
                er.symbol_raw = " & ".join(m[0] for m in matches)
                er.symbol = None  # ambiguous – let caller decide

        if not er.symbols:
            fallback = (
                _ENTRY_SYMBOL_FALLBACK_RE.search(upper)
                or _PCT_ENTRY_SYMBOL_FALLBACK_RE.search(upper)
                or _IN_SYMBOL_FALLBACK_RE.search(upper)
            )
            if fallback:
                candidate = fallback.group(1).strip().upper()
                if candidate not in _SYMBOL_FALLBACK_STOPWORDS and not candidate[0].isdigit():
                    er.symbol_raw = candidate
                    er.symbol = candidate
                    er.symbols = [candidate]

        # ---- Contract month ------------------------------------------------
        m = _CONTRACT_MONTH_RE.search(text)
        if m:
            raw_month = m.group(1).upper()
            er.contract_month = _MONTH_NORM.get(raw_month, raw_month[:3])

        # ---- Option type and strike ----------------------------------------
        ot = _OPTION_TYPE_RE.search(text)
        if ot:
            er.option_type = ot.group(1).upper()

        sk = _STRIKE_RE.search(text)
        if sk:
            val = sk.group(1) or sk.group(2)
            if val:
                er.strike_price = _clean_num(val)

        # ---- Percentage ----------------------------------------------------
        pct_matches = _PCT_RE.findall(text)
        if pct_matches:
            # Take the first valid percentage in range [0, 100]
            for p in pct_matches:
                v = _clean_num(p)
                if v is not None and Decimal("0") <= v <= Decimal("100"):
                    er.quantity_percent = v
                    break

        # ---- Execution prices ----------------------------------------------
        # Exclude numbers that look like stop-loss or target (they come after SL/TGT keywords)
        # Strategy: scan for "@" or "at" patterns that are NOT preceded by SL/TGT keywords
        entry_matches = _ENTRY_RE.finditer(text)
        for em in entry_matches:
            v = _clean_num(em.group(1))
            if v is not None:
                er.execution_prices.append(v)
        if er.execution_prices:
            er.execution_price_primary = er.execution_prices[0]

        # ---- Stop loss -----------------------------------------------------
        sl = _SL_RE.search(text)
        if sl:
            er.stop_loss = _clean_num(sl.group(1))

        # ---- Targets -------------------------------------------------------
        tgt = _TGT_RE.search(text)
        if tgt:
            er.targets = _parse_price_list(tgt.group(1))

        return er
