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
import re
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
from src.schemas import ParsedMessageBundle, ParsedTradeEvent
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

_BASIS_COMPAT_MAP = {
    "MODEL_ALLOCATION": "CUSTOMER_BUYING_CAPACITY",
    "CURRENT_HOLDING": "CURRENT_POSITION",
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

    def parse_bundle(
        self,
        raw_text: str,
        source_message_id: Optional[str] = None,
        existing_long: bool = False,
        existing_short: bool = False,
        has_holdings_context: bool = False,
        market_group: Optional[str] = None,
        parent_metadata: Optional[dict] = None,
    ) -> ParsedMessageBundle:
        """
        Parse a source message into a message-level bundle.

        Existing single-clause messages are represented as a one-child bundle.
        Ordered reversals are represented as exactly two child events.
        """
        normalized_parent = normalize(raw_text)
        single = self.parse(
            raw_text=raw_text,
            source_message_id=source_message_id,
            existing_long=existing_long,
            existing_short=existing_short,
            has_holdings_context=has_holdings_context,
            market_group=market_group,
        )

        clauses = _split_structural_clauses(raw_text)
        if len(clauses) <= 1:
            single.clause_text = raw_text
            return ParsedMessageBundle(
                source_message_id=source_message_id,
                raw_text=raw_text,
                normalized_text=normalized_parent,
                child_events=[single],
                auto_apply_eligible=single.auto_apply_eligible,
                needs_review=single.needs_review,
                validation_errors=list(single.validation_errors),
                validation_warnings=list(single.validation_warnings),
            )

        if single.is_correction or single.is_conditional:
            return _review_bundle(
                single,
                source_message_id,
                raw_text,
                normalized_parent,
                "ORDERED_SPLIT_AMBIGUOUS",
            )

        actionable = [clause for clause in clauses if _has_actionable_text(clause)]
        if len(actionable) > 2:
            return _review_bundle(
                single,
                source_message_id,
                raw_text,
                normalized_parent,
                "ORDERED_TOO_MANY_ACTIONABLE_CLAUSES",
            )
        if len(actionable) != 2:
            return _review_bundle(
                single,
                source_message_id,
                raw_text,
                normalized_parent,
                "ORDERED_SPLIT_AMBIGUOUS",
            )

        if existing_long and existing_short:
            return _review_bundle(
                single,
                source_message_id,
                raw_text,
                normalized_parent,
                "ORDERED_SPLIT_AMBIGUOUS",
            )

        old_side = "LONG" if existing_long else "SHORT" if existing_short else None
        child1 = self.parse(
            raw_text=actionable[0],
            source_message_id=_child_source_id(source_message_id, 1),
            existing_long=existing_long,
            existing_short=existing_short,
            has_holdings_context=has_holdings_context,
            market_group=market_group,
        )
        child2 = self.parse(
            raw_text=actionable[1],
            source_message_id=_child_source_id(source_message_id, 2),
            existing_long=False,
            existing_short=False,
            has_holdings_context=has_holdings_context,
            market_group=market_group,
        )

        _prepare_ordered_child(child1, raw_text, actionable[0], source_message_id, 1)
        _prepare_ordered_child(child2, raw_text, actionable[1], source_message_id, 2)
        if old_side:
            child1.direction = old_side
            child1.resolved_position_side = old_side

        reason = _validate_ordered_children(child1, child2, old_side)
        if reason:
            bundle = ParsedMessageBundle(
                source_message_id=source_message_id,
                raw_text=raw_text,
                normalized_text=normalized_parent,
                is_ordered=True,
                bundle_type="ORDERED_REVERSAL",
                child_events=[child1, child2],
                validation_errors=[reason],
                auto_apply_eligible=False,
                needs_review=True,
            )
            for child in bundle.child_events:
                child.auto_apply_eligible = False
                child.needs_review = True
                child.validation_errors.append(reason)
            return bundle

        return ParsedMessageBundle(
            source_message_id=source_message_id,
            raw_text=raw_text,
            normalized_text=normalized_parent,
            is_ordered=True,
            bundle_type="ORDERED_REVERSAL",
            child_events=[child1, child2],
            auto_apply_eligible=True,
            needs_review=False,
        )


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
    event.semantics_version = "v2"
    event.surface_instruction = rule_result.surface_instruction
    event.entry_capacity_pct = rule_result.entry_capacity_pct
    event.reduction_pct = rule_result.reduction_pct
    event.position_effect = rule_result.position_effect
    event.resolved_position_side = rule_result.resolved_position_side

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
        event.quantity_basis = _BASIS_COMPAT_MAP.get(
            rule_result.quantity_basis,
            rule_result.quantity_basis,
        )
    
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
    if action in ("OPEN_LONG", "ADD_LONG"):
        event.resolved_position_side = event.resolved_position_side or "LONG"
        event.surface_instruction = event.surface_instruction or "BUY"
    if action in ("OPEN_SHORT", "ADD_SHORT"):
        event.resolved_position_side = event.resolved_position_side or "SHORT"
        event.surface_instruction = event.surface_instruction or "SELL"

    # Default allocation for entry/add when no percentage was extracted
    if action in ("OPEN_LONG", "OPEN_SHORT", "ADD_LONG", "ADD_SHORT"):
        if event.quantity_percent is None:
            event.quantity_percent = Decimal(DEFAULT_ENTRY_ALLOCATION_PCT)
        event.quantity_basis = "CUSTOMER_BUYING_CAPACITY"
        event.entry_capacity_pct = event.quantity_percent
        event.position_effect = event.position_effect or "OPEN"

    # Default part profit percentage (rule should have set this, but safety net)
    if action == "REDUCE_POSITION" and event.quantity_percent is None:
        event.quantity_percent = Decimal(DEFAULT_PART_PROFIT_PCT)
        event.quantity_basis = "CURRENT_POSITION"
        event.reduction_pct = event.quantity_percent
        event.position_effect = event.position_effect or "DECREASE"
        event.validation_warnings.append("Quantity not extracted; defaulted to 25% part profit.")
    elif action == "REDUCE_POSITION":
        event.quantity_basis = "CURRENT_POSITION"
        event.reduction_pct = event.reduction_pct or event.quantity_percent
        event.position_effect = event.position_effect or "DECREASE"

    # Remaining holding multiplier for reduce
    if action == "REDUCE_POSITION" and event.quantity_percent is not None:
        if event.remaining_holding_multiplier is None:
            event.remaining_holding_multiplier = Decimal("1") - event.quantity_percent / Decimal("100")

    # Close always zeroes
    if action == "CLOSE_POSITION":
        event.quantity_basis = "CURRENT_POSITION"
        event.position_effect = event.position_effect or "CLOSE"
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


def _child_source_id(parent_id: Optional[str], index: int) -> Optional[str]:
    if not parent_id:
        return None
    return f"{parent_id}#{index}"


def _split_structural_clauses(raw_text: str) -> list[str]:
    text = (raw_text or "").replace("\\n", "\n")
    parts = [part.strip() for part in re.split(r"\s*(?:&|;|\n+)\s*", text) if part.strip()]
    return parts


def _has_actionable_text(clause: str) -> bool:
    return bool(re.search(
        r"\b(BUY|SELL|FULL\s+PROFIT|BOOK\s+FULL\s+PROFIT|EXIT\s+FROM|SL\s+TOUCH(?:ED)?|STOP\s+LOSS\s+HIT|PART\s+PROFIT|PROFIT\s+BOOK)\b",
        clause,
        re.IGNORECASE,
    ))


def _prepare_ordered_child(
    child: ParsedTradeEvent,
    parent_raw_text: str,
    clause_text: str,
    parent_source_message_id: Optional[str],
    index: int,
) -> None:
    child.raw_text = parent_raw_text
    child.clause_text = clause_text
    child.normalized_text = normalize(clause_text)
    child.parent_source_message_id = parent_source_message_id
    child.child_event_index = index
    child.is_multi_instrument = False


def _review_bundle(
    event: ParsedTradeEvent,
    source_message_id: Optional[str],
    raw_text: str,
    normalized_text: str,
    reason: str,
) -> ParsedMessageBundle:
    event.needs_review = True
    event.auto_apply_eligible = False
    event.validation_errors.append(reason)
    return ParsedMessageBundle(
        source_message_id=source_message_id,
        raw_text=raw_text,
        normalized_text=normalized_text,
        child_events=[event],
        validation_errors=[reason],
        auto_apply_eligible=False,
        needs_review=True,
    )


def _same_non_direction_identity(a: ParsedTradeEvent, b: ParsedTradeEvent) -> bool:
    return (
        a.market_group == b.market_group
        and a.symbol == b.symbol
        and (a.contract_month or "") == (b.contract_month or "")
        and (a.option_type or "") == (b.option_type or "")
        and str(a.strike_price or "") == str(b.strike_price or "")
    )


def _validate_ordered_children(
    child1: ParsedTradeEvent,
    child2: ParsedTradeEvent,
    old_side: Optional[str],
) -> Optional[str]:
    if child1.is_correction or child1.is_conditional or child2.is_correction or child2.is_conditional:
        return "ORDERED_SPLIT_AMBIGUOUS"
    if not child1.symbol or not child2.symbol:
        return "ORDERED_SPLIT_AMBIGUOUS"
    if child1.final_action != "CLOSE_POSITION":
        return "ORDERED_CHILD_1_NOT_FULL_CLOSE"
    if child1.surface_instruction not in {"BOOK_FULL", "SL_TOUCH", "EXIT"}:
        return "ORDERED_CHILD_1_NOT_FULL_CLOSE"
    if child2.final_action not in {"OPEN_LONG", "OPEN_SHORT", "ADD_LONG", "ADD_SHORT"}:
        return "ORDERED_CHILD_2_INVALID_ENTRY"
    if not child2.entry_capacity_pct or child2.quantity_basis != "CUSTOMER_BUYING_CAPACITY":
        return "ORDERED_CHILD_2_INVALID_ENTRY"
    if not _same_non_direction_identity(child1, child2):
        return "ORDERED_CHILD_IDENTITY_MISMATCH"
    if old_side == "LONG" and child2.resolved_position_side != "SHORT":
        return "ORDERED_NOT_OPPOSITE_DIRECTION"
    if old_side == "SHORT" and child2.resolved_position_side != "LONG":
        return "ORDERED_NOT_OPPOSITE_DIRECTION"
    if old_side is None:
        return "ORDERED_CHILD_1_NO_POSITION"
    return None
