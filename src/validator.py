"""
validator.py
============
Pre-engine validation layer (Layer 5).

Validates a ParsedTradeEvent *before* it is passed to the holdings engine.
Enriches validation_errors and validation_warnings.
Sets final auto_apply_eligible flag.

This is the last safety check before any position change.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from src.config import AUTO_APPLY_THRESHOLD, REVIEW_THRESHOLD
from src.schemas import ParsedTradeEvent


# Actions that are safe to process without auto-applying to holdings
_STATUS_ACTIONS = {"HOLD_POSITION", "TARGET_HIT", "UPDATE_STOP_LOSS"}

# Actions that are always review-only
_ALWAYS_REVIEW = {
    "AMBIGUOUS", "CORRECTION", "CANCEL_PREVIOUS", "CONDITIONAL_INSTRUCTION",
    "CLOSE_GROUP", "SPLIT_REQUIRED", "UNKNOWN",
}

# Entry actions that need a symbol
_NEEDS_SYMBOL = {
    "OPEN_LONG", "OPEN_SHORT", "ADD_LONG", "ADD_SHORT",
    "REDUCE_POSITION", "CLOSE_POSITION",
    "HOLD_POSITION", "UPDATE_STOP_LOSS", "TARGET_HIT",
}

_ENTRY_ACTIONS = {"OPEN_LONG", "OPEN_SHORT", "ADD_LONG", "ADD_SHORT"}
_SYMBOL_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9._-]{1,14}\b")
_SYMBOL_STOPWORDS = {
    "BUY", "SELL", "AT", "CMP", "ABOVE", "BELOW", "IF", "WHEN", "WITH",
    "SL", "S", "L", "STOP", "LOSS", "TGT", "TARGET", "PROFIT", "BOOK",
    "FULL", "PART", "EXIT", "FROM", "POSITION", "POSITIONS", "LONG", "SHORT",
    "WAIT", "CONFIRMATION", "CONFIRM",
}


def _symbol_like_tokens(raw_text: str) -> list[str]:
    tokens = []
    for match in _SYMBOL_TOKEN_RE.finditer((raw_text or "").upper()):
        token = match.group(0)
        if token in _SYMBOL_STOPWORDS or token[0].isdigit():
            continue
        tokens.append(token)
    return list(dict.fromkeys(tokens))


def _looks_like_unsupported_multi_instrument_single(event: ParsedTradeEvent) -> bool:
    if event.is_multi_instrument:
        return False
    if len(event.symbols or []) != 1 or not event.symbol:
        return False
    if event.final_action not in _ENTRY_ACTIONS:
        return False

    raw_symbols = _symbol_like_tokens(event.raw_text)
    extra_symbols = [token for token in raw_symbols if token != event.symbol]
    if not extra_symbols:
        return False

    has_extra_trade_structure = (
        len(event.execution_prices) > 1
        or len(re.findall(r"\b(?:s/?l|stoploss|stop\s+loss|stop)\b", event.raw_text or "", re.IGNORECASE)) > 1
        or len(re.findall(r"\b(?:tgt|target|t1|t2|1st\s+target|2nd\s+target)\b", event.raw_text or "", re.IGNORECASE)) > 1
        or bool(re.search(r"(?:,|&|\bAND\b)\s*" + re.escape(extra_symbols[0]) + r"\b", event.raw_text or "", re.IGNORECASE))
    )
    return has_extra_trade_structure


def validate(
    event: ParsedTradeEvent,
    confidence: Optional[float] = None,
    already_processed: bool = False,
) -> ParsedTradeEvent:
    """
    Validate *event* in place.

    Appends to event.validation_errors and event.validation_warnings.
    Sets event.auto_apply_eligible and event.needs_review.

    Returns the event (mutated).
    """
    errors = event.validation_errors
    warnings = event.validation_warnings
    conf = confidence if confidence is not None else (event.ml_confidence or 0.0)

    # ---- Idempotency check -------------------------------------------------
    if already_processed:
        event.auto_apply_eligible = False
        event.needs_review = False
        errors.append("Message already processed (duplicate source_message_id).")
        return event

    # ---- Always-review actions ---------------------------------------------
    if event.final_action in _ALWAYS_REVIEW:
        event.auto_apply_eligible = False
        event.needs_review = True
        return event

    # ---- Symbol check ------------------------------------------------------
    if event.final_action in _NEEDS_SYMBOL and not event.symbol:
        if event.is_multi_instrument and event.symbols:
            warnings.append("Multi-instrument: symbol is ambiguous until split.")
        else:
            errors.append("Symbol is missing; cannot apply automatically.")
            event.auto_apply_eligible = False
            event.needs_review = True
            return event

    # ---- Multi-instrument --------------------------------------------------
    if event.is_multi_instrument:
        event.auto_apply_eligible = False
        event.needs_review = True
        warnings.append("Multi-instrument message must be split before applying.")
        return event

    if _looks_like_unsupported_multi_instrument_single(event):
        event.auto_apply_eligible = False
        event.needs_review = True
        warnings.append(
            "Possible unsupported multi-instrument entry parsed as a single event; manual review required."
        )
        return event

    # ---- Percentage range --------------------------------------------------
    if event.quantity_percent is not None:
        if event.quantity_percent < Decimal("0") or event.quantity_percent > Decimal("100"):
            errors.append(f"Percentage {event.quantity_percent} is outside [0, 100].")
            event.auto_apply_eligible = False
            event.needs_review = True
            return event

    if (
        event.final_action in _ENTRY_ACTIONS
        and event.quantity_basis == "CUSTOMER_BUYING_CAPACITY"
        and event.quantity_percent is None
    ):
        errors.append("Entry capacity missing; cannot apply entry without quantity_percent.")
        event.auto_apply_eligible = False
        event.needs_review = True
        return event

    # ---- Requires context --------------------------------------------------
    if event.requires_context or event.is_correction or event.is_conditional:
        event.auto_apply_eligible = False
        event.needs_review = True
        warnings.append("Event requires context; not auto-applied.")
        return event

    # ---- Action–direction consistency -------------------------------------
    action = event.final_action
    direction = event.direction

    if action == "OPEN_LONG" and direction and direction != "LONG":
        errors.append(f"Action OPEN_LONG but direction={direction}.")
        event.needs_review = True

    if action == "OPEN_SHORT" and direction and direction != "SHORT":
        errors.append(f"Action OPEN_SHORT but direction={direction}.")
        event.needs_review = True

    if action == "ADD_LONG" and direction and direction != "LONG":
        errors.append(f"Action ADD_LONG but direction={direction}.")
        event.needs_review = True

    if action == "ADD_SHORT" and direction and direction != "SHORT":
        errors.append(f"Action ADD_SHORT but direction={direction}.")
        event.needs_review = True

    # ---- Confidence threshold gate ----------------------------------------
    # Note: deterministic rule actions (resolution_source=RULE) have
    # confidence=1.0 and should not be blocked by this gate.
    if event.resolution_source == "ML" and conf < REVIEW_THRESHOLD:
        event.auto_apply_eligible = False
        event.needs_review = True
        warnings.append(
            f"ML confidence {conf:.3f} below review threshold {REVIEW_THRESHOLD}."
        )
        return event

    # ---- No validation errors → check final gate --------------------------
    if errors:
        event.auto_apply_eligible = False
        event.needs_review = True
        return event

    # Eligibility: confidence >= AUTO_APPLY_THRESHOLD OR rule-based (conf=1.0)
    effective_conf = 1.0 if event.resolution_source in ("RULE", "RULE_AND_ML_AGREE") else conf
    if effective_conf >= AUTO_APPLY_THRESHOLD and not event.needs_review:
        event.auto_apply_eligible = True
    else:
        event.auto_apply_eligible = False
        event.needs_review = True

    return event
