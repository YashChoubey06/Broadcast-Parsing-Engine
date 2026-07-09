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
