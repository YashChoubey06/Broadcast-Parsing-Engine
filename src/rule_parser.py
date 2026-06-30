"""
rule_parser.py
==============
Layer 4 (rule component) of the hybrid parser.

Implements all deterministic business rules R01–R13 from
11_label_and_position_rules.csv, plus the sell-ambiguity rules A/B/C/D.

IMPORTANT: This module returns a *rule_action* and associated fields.
           It does NOT modify holdings.
           It does NOT call the ML classifier.
           The hybrid_parser.py combines this output with ML predictions.

Rule priority (highest first):
 1. Correction flag → CORRECTION (requires_context)
 2. Conditional flag → CONDITIONAL_INSTRUCTION
 3. Ignore/cancel flag → CANCEL_PREVIOUS
 4. SL Touch → CLOSE_POSITION
 5. Full profit / Exit → CLOSE_POSITION
 6. Part/partial profit → REDUCE_POSITION 25%
 7. X% profit book → REDUCE_POSITION X%
 8. Target touch → TARGET_HIT
 9. Hold + SL update → UPDATE_STOP_LOSS
 10. Hold alone → HOLD_POSITION
 11. Buy with direction → OPEN_LONG / ADD_LONG
 12. Sell with context → OPEN_SHORT / REDUCE_POSITION / AMBIGUOUS
 13. Group action → CLOSE_GROUP
 14. Multi-instrument → SPLIT_REQUIRED
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from src.entity_extractor import ExtractionResult


# ---------------------------------------------------------------------------
# Re-used patterns (rule-level, applied to UPPER-CASED text)
# ---------------------------------------------------------------------------

_PROFIT_BOOK_PCT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:PROFIT\s*BOOK|SELL)",
    re.IGNORECASE,
)
_BOOK_PROFIT_PCT_RE = re.compile(
    r"(?:BOOK|SELL)\s+(\d+(?:\.\d+)?)\s*%\s*(?:PROFIT)?",
    re.IGNORECASE,
)
_BOOK_PCT_PROFIT_RE = re.compile(
    r"(?:BOOK)\s+(\d+(?:\.\d+)?)\s*%\s*PROFIT",
    re.IGNORECASE,
)
_PLAIN_BUY_RE = re.compile(r"\bBUY\b", re.IGNORECASE)
_AGAIN_ADD_RE = re.compile(
    r"\b(AGAIN\s+BUY|RE-?BUY|ADD\s+LONG|AGAIN\s+ADD)\b",
    re.IGNORECASE,
)
_AGAIN_SHORT_RE = re.compile(
    r"\b(AGAIN\s+SELL|ADD\s+SHORT|ADD\s+SHORT\s+POSITION)\b",
    re.IGNORECASE,
)
_PLAIN_SELL_RE = re.compile(r"\bSELL\b", re.IGNORECASE)
_SHORT_EXPLICIT_RE = re.compile(r"\bSHORT\b", re.IGNORECASE)
_PROFIT_LANG_RE = re.compile(
    r"\b(PROFIT\s*BOOK|BOOK\s*PROFIT|PARTIAL\s*PROFIT|PART\s*PROFIT)\b",
    re.IGNORECASE,
)
_POSITIONAL_SL_RE = re.compile(
    r"\bPOSITIONAL\s+SL\b",
    re.IGNORECASE,
)
_HOLD_WITH_SL_RE = re.compile(
    r"\bHOLD\b.{0,40}\bSL\b|\bHOLD\b.{0,40}\bSTOP\b",
    re.IGNORECASE,
)
_GROUP_EXIT_RE = re.compile(
    r"\bEXIT\s+FROM\b.{0,40}\b(POSITIONS|INDICES|STOCKS)\b",
    re.IGNORECASE,
)


class RuleParseResult:
    """
    Deterministic rule output.
    """
    __slots__ = (
        "rule_action",
        "direction",
        "quantity_percent",
        "quantity_basis",
        "remaining_holding_multiplier",
        "requires_context",
        "is_correction",
        "is_conditional",
        "is_multi_instrument",
        "is_group_action",
        "auto_apply_eligible",
        "needs_review",
        "rule_confidence",
        "rule_notes",
    )

    def __init__(self):
        self.rule_action: Optional[str] = None
        self.direction: Optional[str] = None
        self.quantity_percent: Optional[Decimal] = None
        self.quantity_basis: str = "UNKNOWN"
        self.remaining_holding_multiplier: Optional[Decimal] = None
        self.requires_context: bool = False
        self.is_correction: bool = False
        self.is_conditional: bool = False
        self.is_multi_instrument: bool = False
        self.is_group_action: bool = False
        self.auto_apply_eligible: bool = False
        self.needs_review: bool = False
        self.rule_confidence: float = 0.0
        self.rule_notes: str = ""


def apply_rules(
    text: str,
    extraction: ExtractionResult,
    existing_long: bool = False,
    existing_short: bool = False,
    has_holdings_context: bool = False,
) -> RuleParseResult:
    """
    Apply deterministic business rules to a normalised message.

    Parameters
    ----------
    text:
        Normalised raw message text.
    extraction:
        Entity extraction result (from entity_extractor.py).
    existing_long:
        True if a long position already exists for the symbol.
    existing_short:
        True if a short position already exists for the symbol.
    has_holdings_context:
        True when holdings context (from the DB) is available.

    Returns
    -------
    RuleParseResult with rule_action set (or None if rules are inconclusive).
    """
    r = RuleParseResult()

    # ------------------------------------------------------------------
    # Priority 1: CORRECTION
    # ------------------------------------------------------------------
    if extraction.is_correction:
        r.rule_action = "CORRECTION"
        r.is_correction = True
        r.requires_context = True
        r.auto_apply_eligible = False
        r.needs_review = True
        r.rule_confidence = 1.0
        r.rule_notes = "Correction message; requires prior-event linking."
        return r

    # ------------------------------------------------------------------
    # Priority 2: CONDITIONAL INSTRUCTION
    # ------------------------------------------------------------------
    if extraction.is_conditional:
        r.rule_action = "CONDITIONAL_INSTRUCTION"
        r.is_conditional = True
        r.requires_context = True
        r.auto_apply_eligible = False
        r.needs_review = True
        r.rule_confidence = 1.0
        r.rule_notes = "Conditional instruction; not immediately executed."
        return r

    # ------------------------------------------------------------------
    # Priority 3: IGNORE / CANCEL
    # ------------------------------------------------------------------
    if extraction.has_ignore:
        r.rule_action = "CANCEL_PREVIOUS"
        r.requires_context = True
        r.auto_apply_eligible = False
        r.needs_review = True
        r.rule_confidence = 1.0
        r.rule_notes = "Ignore/cancel message; requires prior-event context."
        return r

    # ------------------------------------------------------------------
    # Priority 4: GROUP ACTION
    # ------------------------------------------------------------------
    if extraction.has_group_action or _GROUP_EXIT_RE.search(text):
        r.rule_action = "CLOSE_GROUP"
        r.is_group_action = True
        r.requires_context = True
        r.auto_apply_eligible = False
        r.needs_review = True
        r.rule_confidence = 1.0
        r.rule_notes = "Group action; manual review required."
        return r

    # ------------------------------------------------------------------
    # Priority 5: SL TOUCH  → CLOSE_POSITION
    # ------------------------------------------------------------------
    if extraction.has_sl_touch:
        r.rule_action = "CLOSE_POSITION"
        r.quantity_percent = Decimal("100")
        r.quantity_basis = "CURRENT_HOLDING"
        r.remaining_holding_multiplier = Decimal("0")
        r.rule_confidence = 1.0
        r.rule_notes = "SL touched; full close of remaining holding."
        # auto_apply set after symbol check in validator
        return r

    # ------------------------------------------------------------------
    # Priority 6: FULL PROFIT / BOOK PROFIT / EXIT  → CLOSE_POSITION
    # ------------------------------------------------------------------
    if extraction.has_full_profit and not extraction.quantity_percent:
        r.rule_action = "CLOSE_POSITION"
        r.quantity_percent = Decimal("100")
        r.quantity_basis = "CURRENT_HOLDING"
        r.remaining_holding_multiplier = Decimal("0")
        r.rule_confidence = 1.0
        r.rule_notes = "Full profit / exit; close entire remaining holding."
        return r

    # ------------------------------------------------------------------
    # Priority 7: PART / PARTIAL PROFIT  → REDUCE_POSITION 25%
    # ------------------------------------------------------------------
    if extraction.has_part_profit:
        r.rule_action = "REDUCE_POSITION"
        r.quantity_percent = Decimal("25")
        r.quantity_basis = "CURRENT_HOLDING"
        r.remaining_holding_multiplier = Decimal("0.75")
        r.rule_confidence = 1.0
        r.rule_notes = "Part profit: 25% of current holding reduced."
        return r

    # ------------------------------------------------------------------
    # Priority 8: X% PROFIT BOOK / BOOK X% PROFIT / SELL X% (profit lang)
    # ------------------------------------------------------------------
    has_profit_lang = bool(_PROFIT_LANG_RE.search(text))
    # Also match "Book X% profit" pattern (percentage is between 'book' and 'profit')
    book_pct_profit_match = _BOOK_PCT_PROFIT_RE.search(text)
    pct_book_match = _PROFIT_BOOK_PCT_RE.search(text) or _BOOK_PROFIT_PCT_RE.search(text)

    # Extract percentage from regex if entity extractor missed it
    _parsed_pct: Optional[Decimal] = extraction.quantity_percent
    if _parsed_pct is None and book_pct_profit_match:
        try:
            _parsed_pct = Decimal(book_pct_profit_match.group(1))
        except Exception:
            pass

    if (has_profit_lang or book_pct_profit_match) and _parsed_pct is not None:
        pct = _parsed_pct
        r.rule_action = "REDUCE_POSITION"
        r.quantity_percent = pct
        r.quantity_basis = "CURRENT_HOLDING"
        r.remaining_holding_multiplier = Decimal("1") - pct / Decimal("100")
        r.rule_confidence = 1.0
        r.rule_notes = f"{pct}% profit book: reduce current holding."
        return r

    # ------------------------------------------------------------------
    # Priority 9: TARGET TOUCH  → TARGET_HIT (status only)
    # ------------------------------------------------------------------
    if extraction.has_target_touch:
        r.rule_action = "TARGET_HIT"
        r.quantity_basis = "NONE"
        r.rule_confidence = 1.0
        r.rule_notes = "Target hit status event; no automatic holding change."
        return r

    # ------------------------------------------------------------------
    # Priority 10: HOLD WITH SL / POSITIONAL SL  → UPDATE_STOP_LOSS
    # ------------------------------------------------------------------
    if extraction.has_hold and extraction.stop_loss is not None:
        r.rule_action = "UPDATE_STOP_LOSS"
        r.quantity_basis = "NONE"
        r.rule_confidence = 1.0
        r.rule_notes = "Hold with SL: update stop loss only."
        return r

    if _POSITIONAL_SL_RE.search(text):
        r.rule_action = "UPDATE_STOP_LOSS"
        r.quantity_basis = "NONE"
        r.rule_confidence = 1.0
        r.rule_notes = "Positional SL: update stop loss only."
        return r

    # ------------------------------------------------------------------
    # Priority 11: HOLD ALONE  → HOLD_POSITION
    # ------------------------------------------------------------------
    if extraction.has_hold:
        r.rule_action = "HOLD_POSITION"
        r.quantity_basis = "NONE"
        r.rule_confidence = 1.0
        r.rule_notes = "Hold: no quantity change."
        return r

    # ------------------------------------------------------------------
    # Priority 12: AGAIN BUY / ADD LONG  → ADD_LONG
    # ------------------------------------------------------------------
    if _AGAIN_ADD_RE.search(text):
        r.rule_action = "ADD_LONG"
        r.direction = "LONG"
        r.quantity_basis = "MODEL_ALLOCATION"
        r.rule_confidence = 1.0
        r.rule_notes = "Again buy / add long."
        if extraction.quantity_percent:
            r.quantity_percent = extraction.quantity_percent
        return r

    # ------------------------------------------------------------------
    # Priority 13: AGAIN SELL / ADD SHORT  → ADD_SHORT
    # ------------------------------------------------------------------
    if _AGAIN_SHORT_RE.search(text):
        r.rule_action = "ADD_SHORT"
        r.direction = "SHORT"
        r.quantity_basis = "MODEL_ALLOCATION"
        r.rule_confidence = 1.0
        r.rule_notes = "Again sell / add short."
        if extraction.quantity_percent:
            r.quantity_percent = extraction.quantity_percent
        return r

    # ------------------------------------------------------------------
    # Priority 14: PLAIN BUY  → OPEN_LONG (or review if duplicate)
    # ------------------------------------------------------------------
    if _PLAIN_BUY_RE.search(text) and not _PLAIN_SELL_RE.search(text):
        r.rule_action = "OPEN_LONG"
        r.direction = "LONG"
        r.quantity_basis = "MODEL_ALLOCATION"
        if extraction.quantity_percent:
            r.quantity_percent = extraction.quantity_percent
        r.rule_confidence = 0.95
        r.rule_notes = "Buy instruction."
        return r

    # ------------------------------------------------------------------
    # Priority 15: SELL ambiguity resolution (Rules A/B/C/D)
    # ------------------------------------------------------------------
    if _PLAIN_SELL_RE.search(text) or _SHORT_EXPLICIT_RE.search(text):
        return _resolve_sell(text, extraction, existing_long, existing_short, has_holdings_context)

    # ------------------------------------------------------------------
    # Multi-instrument
    # ------------------------------------------------------------------
    if extraction.is_multi_instrument:
        r.rule_action = "SPLIT_REQUIRED"
        r.is_multi_instrument = True
        r.requires_context = True
        r.needs_review = True
        r.rule_confidence = 1.0
        r.rule_notes = "Multiple instruments detected; split or review required."
        return r

    # Rules inconclusive; ML will decide
    return r


def _resolve_sell(
    text: str,
    extraction: ExtractionResult,
    existing_long: bool,
    existing_short: bool,
    has_holdings_context: bool,
) -> RuleParseResult:
    """
    Sell ambiguity resolver (Rules A/B/C/D from spec).

    Rule A: existing long + sell → reduce
    Rule B: profit language → reduce or close
    Rule C: no long + short cues → open short
    Rule D: insufficient context → AMBIGUOUS / manual review
    """
    r = RuleParseResult()

    has_profit_lang = bool(_PROFIT_LANG_RE.search(text))
    has_sl = extraction.stop_loss is not None
    has_target = bool(extraction.targets)
    has_pct = extraction.quantity_percent is not None
    pct = extraction.quantity_percent

    # Rule B: profit language always means reduce/close
    if has_profit_lang:
        if pct:
            r.rule_action = "REDUCE_POSITION"
            r.quantity_percent = pct
            r.quantity_basis = "CURRENT_HOLDING"
            r.remaining_holding_multiplier = Decimal("1") - pct / Decimal("100")
        else:
            r.rule_action = "CLOSE_POSITION"
            r.quantity_percent = Decimal("100")
            r.quantity_basis = "CURRENT_HOLDING"
            r.remaining_holding_multiplier = Decimal("0")
        r.rule_confidence = 1.0
        r.rule_notes = "Profit language: reduce or close existing position."
        return r

    # Rule A: existing long position + sell → reduce
    if existing_long:
        if pct:
            r.rule_action = "REDUCE_POSITION"
            r.quantity_percent = pct
            r.quantity_basis = "CURRENT_HOLDING"
            r.remaining_holding_multiplier = Decimal("1") - pct / Decimal("100")
            r.rule_confidence = 0.97
            r.rule_notes = "Existing long + sell: reduce current holding."
        else:
            # Sell without % on existing long → context check needed
            # If clear short cues (SL above entry for short, downside targets)
            # → might be opposite direction
            if (has_sl or has_target) and _SHORT_EXPLICIT_RE.search(text):
                # Opposite direction conflict
                r.rule_action = "AMBIGUOUS"
                r.requires_context = True
                r.needs_review = True
                r.rule_confidence = 0.5
                r.rule_notes = "Existing long + explicit short cues: opposite direction conflict → review."
            else:
                r.rule_action = "CLOSE_POSITION"
                r.quantity_percent = Decimal("100")
                r.quantity_basis = "CURRENT_HOLDING"
                r.remaining_holding_multiplier = Decimal("0")
                r.rule_confidence = 0.90
                r.rule_notes = "Existing long + plain sell (no %): interpret as close."
        return r

    # Rule C: no long, clear short entry cues → OPEN_SHORT
    short_explicit = bool(_SHORT_EXPLICIT_RE.search(text))
    if (has_sl or has_target or short_explicit) and not existing_long:
        r.rule_action = "OPEN_SHORT"
        r.direction = "SHORT"
        r.quantity_basis = "MODEL_ALLOCATION"
        if pct:
            r.quantity_percent = pct
        r.rule_confidence = 0.90
        r.rule_notes = "No existing long + short cues: open short."
        return r

    # Rule D: insufficient context → AMBIGUOUS
    if not has_holdings_context:
        r.rule_action = "AMBIGUOUS"
        r.requires_context = True
        r.needs_review = True
        r.rule_confidence = 0.0
        r.rule_notes = "Sell with no holdings context and no clear short cues: ambiguous."
        return r

    # Rule D fallback: context available but no long position, no clear cues
    r.rule_action = "AMBIGUOUS"
    r.requires_context = True
    r.needs_review = True
    r.rule_confidence = 0.3
    r.rule_notes = "Sell: holdings context available but no clear direction inference."
    return r
