"""
hybrid_parser.py
================
The main parser entry point (Step 5).

Combines:
  Layer 1 – text_normalizer      (normalisation)
  Layer 2 – ml_classifier        (intent prediction)
  Layer 3 – entity_extractor     (regex entity extraction)
  Layer 4 – rule_parser          (deterministic rule engine)

Produces a validated ParsedTradeEvent.

Resolution priority:
  RULE > ML when a deterministic rule fires.
  ML is primary when rules are inconclusive.
  Conflicting rule and ML predictions go to manual review.

Resolution source values:
  RULE              – deterministic rule fired; ML ignored or agrees
  ML                – rules inconclusive; ML used
  RULE_AND_ML_AGREE – both agree (higher confidence)
  CONTEXT_RESOLVED  – sell resolved using holdings context
  MANUAL_REVIEW     – conflict or insufficient confidence
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Optional

from src.config import (
    AUTO_APPLY_THRESHOLD,
    DEFAULT_ENTRY_ALLOCATION_PCT,
    DEFAULT_PART_PROFIT_PCT,
    PARSER_VERSION,
    REVIEW_THRESHOLD,
)
from src.entity_extractor import EntityExtractor, ExtractionResult
from src.ml_classifier import MLClassifier, get_classifier
from src.rule_parser import RuleParseResult, apply_rules
from src.schemas import ParsedTradeEvent
from src.text_normalizer import normalize


# ---------------------------------------------------------------------------
# Actions that indicate a hold/status event rather than a holding change
# ---------------------------------------------------------------------------
_STATUS_ONLY_ACTIONS = {"HOLD_POSITION", "TARGET_HIT", "UPDATE_STOP_LOSS"}

# Actions that are non-trade record types
_NON_TRADE_ACTIONS = {"NON_TRADE", "ADMIN_NOTICE", "UNKNOWN"}

# Actions that require manual review
_REVIEW_ACTIONS = {
    "AMBIGUOUS", "CORRECTION", "CANCEL_PREVIOUS", "CONDITIONAL_INSTRUCTION",
    "CLOSE_GROUP", "SPLIT_REQUIRED",
}


class HybridParser:
    """
    The complete Step 5 parser.

    Usage::

        parser = HybridParser()
        event = parser.parse("50% PROFIT BOOK IN NIFTY @25942")
        print(event.to_json())
    """

    def __init__(
        self,
        classifier: Optional[MLClassifier] = None,
        extractor: Optional[EntityExtractor] = None,
    ) -> None:
        self._clf = classifier or get_classifier()
        self._extractor = extractor or EntityExtractor()

    def parse(
        self,
        raw_text: str,
        source_message_id: Optional[str] = None,
        existing_long: bool = False,
        existing_short: bool = False,
        has_holdings_context: bool = False,
        market_group: Optional[str] = None,
    ) -> ParsedTradeEvent:
        """
        Parse a raw trade message into a structured ParsedTradeEvent.

        Parameters
        ----------
        raw_text:
            The original unmodified message string.
        source_message_id:
            Unique message identifier for idempotency.
        existing_long:
            Whether a long position currently exists for the primary symbol.
        existing_short:
            Whether a short position currently exists for the primary symbol.
        has_holdings_context:
            True when holdings have been loaded from the database.
        market_group:
            Optional market group from the dataset (passed through to event).
        """
        event = ParsedTradeEvent()
        event.source_message_id = source_message_id
        event.raw_text = raw_text
        event.normalized_text = normalize(raw_text)
        event.market_group = market_group

        # ---- Step 1: Entity extraction ------------------------------------
        extraction: ExtractionResult = self._extractor.extract(event.normalized_text)

        # ---- Step 2: ML prediction ----------------------------------------
        ml_action = "UNKNOWN"
        ml_confidence = 0.0
        if self._clf.is_loaded():
            ml_action, ml_confidence = self._clf.predict(event.normalized_text)
        event.ml_action = ml_action
        event.ml_confidence = ml_confidence

        # ---- Step 3: Deterministic rule evaluation -------------------------
        rule_result: RuleParseResult = apply_rules(
            text=event.normalized_text,
            extraction=extraction,
            existing_long=existing_long,
            existing_short=existing_short,
            has_holdings_context=has_holdings_context,
        )
        event.rule_action = rule_result.rule_action

        # ---- Step 4: Hybrid resolution ------------------------------------
        final_action, resolution_source, final_confidence = _resolve(
            rule_result=rule_result,
            ml_action=ml_action,
            ml_confidence=ml_confidence,
        )

        event.final_action = final_action
        event.resolution_source = resolution_source

        # ---- Step 5: Populate fields from extraction + rule result --------
        _populate_event(event, extraction, rule_result, final_confidence)

        # ---- Step 6: Apply default quantities where needed ----------------
        _apply_defaults(event)

        # ---- Step 7: Determine record_type --------------------------------
        event.record_type = _derive_record_type(event)

        # ---- Step 8: Mark review/auto-apply flags -------------------------
        _set_eligibility_flags(event, rule_result, final_confidence)

        return event

    def parse_multi_instrument(
        self,
        raw_text: str,
        source_message_id: Optional[str] = None,
        **kwargs,
    ) -> list[ParsedTradeEvent]:
        """
        Attempt to split a multi-instrument message into child events.

        Returns a list with one ParsedTradeEvent per instrument if splitting
        is safe, or a single event with needs_review=True if not.
        """
        # First parse the whole message
        parent_event = self.parse(raw_text, source_message_id=source_message_id, **kwargs)

        if not parent_event.is_multi_instrument:
            return [parent_event]

        extraction = self._extractor.extract(parent_event.normalized_text)
        symbols = extraction.symbols

        if not symbols:
            parent_event.needs_review = True
            parent_event.validation_errors.append("Multi-instrument: no symbols extracted.")
            return [parent_event]

        # Conservative split: only when same action applies to every instrument,
        # same percentage, and no per-instrument prices/SL/targets are ambiguous.
        action = parent_event.final_action
        pct = parent_event.quantity_percent
        sl = parent_event.stop_loss
        targets = parent_event.targets
        prices = parent_event.execution_prices

        # Check if splitting is safe
        # Splitting is safe only for close/reduce actions with uniform semantics
        safe_split_actions = {"CLOSE_POSITION", "REDUCE_POSITION", "TARGET_HIT",
                              "HOLD_POSITION", "UPDATE_STOP_LOSS"}

        if action not in safe_split_actions:
            parent_event.needs_review = True
            parent_event.validation_warnings.append(
                f"Multi-instrument split unsafe for action={action}."
            )
            return [parent_event]

        # Split into child events
        children: list[ParsedTradeEvent] = []
        for i, sym in enumerate(symbols):
            child = ParsedTradeEvent()
            child.raw_text = raw_text
            child.normalized_text = parent_event.normalized_text
            child.source_message_id = f"{source_message_id}#{i+1}" if source_message_id else None
            child.parent_source_message_id = source_message_id
            child.child_event_index = i + 1
            child.final_action = action
            child.rule_action = parent_event.rule_action
            child.ml_action = parent_event.ml_action
            child.ml_confidence = parent_event.ml_confidence
            child.resolution_source = parent_event.resolution_source
            child.symbol = sym
            child.symbols = [sym]
            child.symbol_raw = sym
            child.quantity_percent = pct
            child.quantity_basis = parent_event.quantity_basis
            child.remaining_holding_multiplier = parent_event.remaining_holding_multiplier
            child.stop_loss = sl
            child.targets = list(targets)
            child.execution_prices = list(prices)
            child.execution_price_primary = prices[0] if prices else None
            child.is_multi_instrument = False
            child.market_group = parent_event.market_group
            child.record_type = parent_event.record_type
            child.auto_apply_eligible = parent_event.auto_apply_eligible
            child.needs_review = parent_event.needs_review
            children.append(child)

        return children


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve(
    rule_result: RuleParseResult,
    ml_action: str,
    ml_confidence: float,
) -> tuple[str, str, float]:
    """
    Decide the final_action, resolution_source, and effective confidence.

    Returns (final_action, resolution_source, confidence).
    """
    rule_action = rule_result.rule_action
    rule_conf = rule_result.rule_confidence

    # Rule fired with high confidence → rule wins
    if rule_action and rule_conf >= 0.95:
        if ml_action and ml_action == rule_action:
            return rule_action, "RULE_AND_ML_AGREE", 1.0
        else:
            return rule_action, "RULE", 1.0

    # Rule fired with lower confidence → compare with ML
    if rule_action and rule_conf > 0.0:
        if ml_action == rule_action:
            return rule_action, "RULE_AND_ML_AGREE", max(rule_conf, ml_confidence)
        elif ml_confidence >= AUTO_APPLY_THRESHOLD:
            # ML confident but rule partially disagrees → prefer rule, flag discrepancy
            return rule_action, "RULE", rule_conf
        else:
            # Both uncertain → manual review
            return rule_action or ml_action or "AMBIGUOUS", "MANUAL_REVIEW", min(rule_conf, ml_confidence)

    # No rule fired → use ML
    if ml_action and ml_action != "UNKNOWN":
        return ml_action, "ML", ml_confidence

    return "AMBIGUOUS", "MANUAL_REVIEW", 0.0


def _populate_event(
    event: ParsedTradeEvent,
    extraction: ExtractionResult,
    rule_result: RuleParseResult,
    confidence: float,
) -> None:
    """Copy entity and rule fields into the event."""
    event.symbol_raw = extraction.symbol_raw
    event.symbol = extraction.symbol
    event.symbols = list(extraction.symbols)
    event.contract_month = extraction.contract_month
    event.strike_price = extraction.strike_price
    event.option_type = extraction.option_type
    event.is_correction = extraction.is_correction or rule_result.is_correction
    event.is_conditional = extraction.is_conditional or rule_result.is_conditional
    event.is_multi_instrument = extraction.is_multi_instrument or rule_result.is_multi_instrument
    event.requires_context = rule_result.requires_context
    event.needs_review = rule_result.needs_review

    # Prices
    event.execution_prices = list(extraction.execution_prices)
    event.execution_price_primary = extraction.execution_price_primary
    event.stop_loss = extraction.stop_loss
    event.targets = list(extraction.targets)

    # Direction: rule takes priority, fallback to extraction hint
    event.direction = rule_result.direction or (
        extraction.direction_hint if extraction.direction_hint else None
    )

    # Quantity
    if rule_result.quantity_percent is not None:
        event.quantity_percent = rule_result.quantity_percent
    elif extraction.quantity_percent is not None:
        event.quantity_percent = extraction.quantity_percent

    if rule_result.quantity_basis and rule_result.quantity_basis != "UNKNOWN":
        event.quantity_basis = rule_result.quantity_basis
    
    if rule_result.remaining_holding_multiplier is not None:
        event.remaining_holding_multiplier = rule_result.remaining_holding_multiplier

    event.ml_confidence = confidence if event.resolution_source == "ML" else event.ml_confidence


def _apply_defaults(event: ParsedTradeEvent) -> None:
    """Apply default quantities where the rules allow them."""
    action = event.final_action

    # Default direction for long-side actions
    if action in ("OPEN_LONG", "ADD_LONG") and event.direction is None:
        event.direction = "LONG"
    if action in ("OPEN_SHORT", "ADD_SHORT") and event.direction is None:
        event.direction = "SHORT"

    # Default allocation for entry/add when no percentage was extracted
    if action in ("OPEN_LONG", "OPEN_SHORT", "ADD_LONG", "ADD_SHORT"):
        if event.quantity_percent is None:
            event.quantity_percent = Decimal(DEFAULT_ENTRY_ALLOCATION_PCT)
            event.quantity_basis = "MODEL_ALLOCATION"

    # Default part profit percentage (rule should have set this, but safety net)
    if action == "REDUCE_POSITION" and event.quantity_percent is None:
        event.quantity_percent = Decimal(DEFAULT_PART_PROFIT_PCT)
        event.quantity_basis = "CURRENT_HOLDING"
        event.validation_warnings.append("Quantity not extracted; defaulted to 25% part profit.")

    # Remaining holding multiplier for reduce
    if action == "REDUCE_POSITION" and event.quantity_percent is not None:
        if event.remaining_holding_multiplier is None:
            event.remaining_holding_multiplier = Decimal("1") - event.quantity_percent / Decimal("100")

    # Close always zeroes
    if action == "CLOSE_POSITION":
        event.remaining_holding_multiplier = Decimal("0")

    # Status-only actions
    if action in _STATUS_ONLY_ACTIONS:
        event.quantity_basis = "NONE"


def _derive_record_type(event: ParsedTradeEvent) -> str:
    """Infer record_type from final_action."""
    action = event.final_action
    if action in _NON_TRADE_ACTIONS:
        return "NON_TRADE"
    if action in {"CONDITIONAL_INSTRUCTION", "CANCEL_PREVIOUS", "CLOSE_GROUP", "CORRECTION"}:
        return "TRADE_CONTROL"
    if action in _STATUS_ONLY_ACTIONS:
        return "TRADE_UPDATE"
    if action == "AMBIGUOUS":
        return "TRADE_CONTROL"
    return "TRADE_ACTION"


def _set_eligibility_flags(
    event: ParsedTradeEvent,
    rule_result: RuleParseResult,
    confidence: float,
) -> None:
    """
    Set auto_apply_eligible and needs_review on the event based on all
    validation checks defined in the spec.
    """
    reasons = list(event.validation_errors)

    # Already flagged as review
    if event.needs_review or rule_result.needs_review:
        event.needs_review = True

    # Actions that can never auto-apply
    if event.final_action in _REVIEW_ACTIONS:
        event.auto_apply_eligible = False
        event.needs_review = True
        return

    # Missing symbol
    if not event.symbol and not event.is_multi_instrument:
        event.validation_warnings.append("Symbol missing; cannot auto-apply.")
        event.needs_review = True
        event.auto_apply_eligible = False
        return

    # Invalid percentage
    if event.quantity_percent is not None:
        if event.quantity_percent < Decimal("0") or event.quantity_percent > Decimal("100"):
            reasons.append(f"Percentage out of range: {event.quantity_percent}")
            event.needs_review = True

    # Requires context
    if event.requires_context:
        event.auto_apply_eligible = False
        event.needs_review = True
        return

    # Confidence gate
    if confidence < REVIEW_THRESHOLD:
        event.auto_apply_eligible = False
        event.needs_review = True
        return

    if confidence >= AUTO_APPLY_THRESHOLD and not event.needs_review:
        event.auto_apply_eligible = True
    else:
        event.auto_apply_eligible = False
        event.needs_review = True
