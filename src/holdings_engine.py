"""
holdings_engine.py
==================
Step 6: Deterministic Holdings Engine.

RECEIVES: Only validated ParsedTradeEvent objects from the validator.
PRODUCES: Updated holdings state + an immutable trade-event record.
NEVER:    Performs text parsing, runs ML models, or makes holdings changes
          based on NLP predictions alone.

All allocation arithmetic uses Python's Decimal for exact results.

Reduction formula (spec):
    new_holding = current_holding * (1 - reduction_percentage / Decimal("100"))

Example chain (must be exact):
    100 → 50 → 25 → 12.5 → 6.25

Position key:
    (portfolio_id, market_group, symbol, contract_month,
     option_type, strike_price, direction)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from src.config import (
    DEFAULT_ENTRY_ALLOCATION_PCT,
    DEFAULT_PORTFOLIO_ID,
    PARSER_VERSION,
    ZERO_POSITION_TOLERANCE,
    STATUS_APPLIED,
    STATUS_MANUAL_REVIEW,
    STATUS_STATUS_ONLY,
)
from src.repository_interfaces import (
    PositionRepository,
    ProcessedMessageRepository,
    ReviewQueueRepository,
    SnapshotRepository,
    TradeEventRepository,
)
from src.position_identity import IdentityMatchStatus, IdentityResolutionResult, identity_from_values
from src.schemas import HoldingsResult, ParsedTradeEvent, PositionState, ReviewItem

# Tolerance for "close enough to zero → treat as closed"
_ZERO_TOL = Decimal(ZERO_POSITION_TOLERANCE)
_DEFAULT_ALLOC = Decimal(DEFAULT_ENTRY_ALLOCATION_PCT)

# Actions that must touch an existing position (not create one)
_REQUIRES_EXISTING = {
    "REDUCE_POSITION", "CLOSE_POSITION", "HOLD_POSITION",
    "UPDATE_STOP_LOSS", "TARGET_HIT",
}

# Actions that only update the stop loss / record status
_STATUS_ONLY = {"HOLD_POSITION", "TARGET_HIT", "UPDATE_STOP_LOSS"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pos_to_dict(pos: Optional[PositionState]) -> Optional[dict]:
    if pos is None:
        return None
    return {
        "position_id": pos.position_id,
        "symbol": pos.symbol,
        "direction": pos.direction,
        "current_allocation_pct": str(pos.current_allocation_pct),
        "average_entry_price": str(pos.average_entry_price) if pos.average_entry_price else None,
        "stop_loss": str(pos.stop_loss) if pos.stop_loss else None,
        "status": pos.status,
        "version": pos.version,
    }


class HoldingsEngine:
    """
    The deterministic holdings engine.

    All methods are pure business logic.  Persistence is delegated to the
    injected repository interfaces.
    """

    def __init__(
        self,
        position_repo: PositionRepository,
        event_repo: TradeEventRepository,
        processed_repo: ProcessedMessageRepository,
        review_repo: ReviewQueueRepository,
        snapshot_repo: SnapshotRepository,
        portfolio_id: str = DEFAULT_PORTFOLIO_ID,
    ) -> None:
        self._positions = position_repo
        self._events = event_repo
        self._processed = processed_repo
        self._review = review_repo
        self._snapshots = snapshot_repo
        self._portfolio_id = portfolio_id

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def apply_event(
        self,
        event: ParsedTradeEvent,
        processing_order: int = 0,
    ) -> HoldingsResult:
        """
        Apply a validated ParsedTradeEvent to the holdings.

        This method is the ONLY entry point for changing positions.
        It is completely deterministic given its inputs.

        Returns a HoldingsResult describing what happened.
        """
        msg_id = event.source_message_id or ""

        # ---- Idempotency check -------------------------------------------
        if msg_id and self._processed.is_processed(msg_id):
            return HoldingsResult(
                status="ALREADY_PROCESSED",
                message=f"Message {msg_id} was already processed.",
            )

        action = event.final_action

        # ---- Route by action ---------------------------------------------
        try:
            result = self._route(event, action, processing_order)
        except Exception as exc:
            # Do NOT partially commit – the transaction wrapper handles rollback
            return HoldingsResult(
                status="ERROR",
                message=f"Engine error: {exc}",
            )

        return result

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def _route(
        self,
        event: ParsedTradeEvent,
        action: str,
        processing_order: int,
    ) -> HoldingsResult:
        if action == "OPEN_LONG":
            return self._handle_open(event, "LONG", processing_order)
        elif action == "OPEN_SHORT":
            return self._handle_open(event, "SHORT", processing_order)
        elif action == "ADD_LONG":
            return self._handle_add(event, "LONG", processing_order)
        elif action == "ADD_SHORT":
            return self._handle_add(event, "SHORT", processing_order)
        elif action == "REDUCE_POSITION":
            return self._handle_reduce(event, processing_order)
        elif action == "CLOSE_POSITION":
            return self._handle_close(event, processing_order)
        elif action in _STATUS_ONLY:
            return self._handle_status_only(event, action, processing_order)
        else:
            # Should not reach here if validator passed – route to review
            return self._send_to_review(
                event,
                reason=f"Unhandled action: {action}",
                processing_order=processing_order,
            )

    # ------------------------------------------------------------------
    # OPEN_LONG / OPEN_SHORT
    # ------------------------------------------------------------------
    def _handle_open(
        self,
        event: ParsedTradeEvent,
        direction: str,
        processing_order: int,
    ) -> HoldingsResult:
        symbol = event.symbol or ""
        same_result = self._resolve_position(event, direction)
        if same_result.status in {
            IdentityMatchStatus.AMBIGUOUS_MATCH,
            IdentityMatchStatus.IDENTITY_CONFLICT,
        }:
            return self._send_to_review(
                event,
                reason=self._identity_review_reason(same_result),
                processing_order=processing_order,
            )
        existing = same_result.matched_position

        # Opposite-direction conflict
        opposite = "SHORT" if direction == "LONG" else "LONG"
        opposite_result = self._resolve_position(event, opposite)
        if opposite_result.status in {
            IdentityMatchStatus.AMBIGUOUS_MATCH,
            IdentityMatchStatus.IDENTITY_CONFLICT,
        }:
            return self._send_to_review(
                event,
                reason=self._identity_review_reason(opposite_result),
                processing_order=processing_order,
            )
        existing_opp = opposite_result.matched_position
        if existing_opp and existing_opp.status == "OPEN":
            return self._send_to_review(
                event,
                reason=(
                    f"Opposite-direction conflict: existing {opposite} position "
                    f"for {symbol} is open."
                ),
                processing_order=processing_order,
            )

        # V2 semantics: repeated same-side entries increase exposure.
        if existing and existing.status == "OPEN":
            return self._handle_add(event, direction, processing_order)

        # Create new position, or reopen a closed full-identity row when an
        # ordered close-then-entry re-enters the same side.
        alloc = event.quantity_percent or _DEFAULT_ALLOC
        pos_before = self._find_closed_full_identity_position(event, direction)
        pos = PositionState(
            position_id=pos_before.position_id if pos_before else None,
            portfolio_id=self._portfolio_id,
            market_group=event.market_group,
            symbol=symbol,
            contract_month=event.contract_month,
            option_type=event.option_type,
            strike_price=event.strike_price,
            direction=direction,
            current_allocation_pct=alloc,
            average_entry_price=event.execution_price_primary,
            stop_loss=event.stop_loss,
            targets=list(event.targets),
            status="OPEN",
            opened_at=_now_iso(),
            updated_at=_now_iso(),
            version=(pos_before.version + 1) if pos_before else 1,
        )
        pos = self._positions.save_position(pos)
        return self._finalise(event, pos_before, pos, processing_order, STATUS_APPLIED)

    # ------------------------------------------------------------------
    # ADD_LONG / ADD_SHORT
    # ------------------------------------------------------------------
    def _handle_add(
        self,
        event: ParsedTradeEvent,
        direction: str,
        processing_order: int,
    ) -> HoldingsResult:
        existing = self._get_position(event, direction)
        if not existing or existing.status != "OPEN":
            # No existing position → open a new one instead
            return self._handle_open(event, direction, processing_order)

        pos_before = self._clone(existing)
        added = event.quantity_percent or _DEFAULT_ALLOC
        new_alloc = existing.current_allocation_pct + added

        # Weighted average entry price
        new_price = event.execution_price_primary
        if new_price and existing.average_entry_price:
            # Weight by allocation units
            old_w = existing.current_allocation_pct
            new_w = added
            total_w = old_w + new_w
            if total_w > Decimal("0"):
                existing.average_entry_price = (
                    existing.average_entry_price * old_w + new_price * new_w
                ) / total_w
        elif new_price:
            existing.average_entry_price = new_price

        existing.current_allocation_pct = new_alloc
        existing.updated_at = _now_iso()
        existing.version += 1
        if event.stop_loss:
            existing.stop_loss = event.stop_loss

        pos = self._positions.save_position(existing)
        return self._finalise(event, pos_before, pos, processing_order, STATUS_APPLIED)

    # ------------------------------------------------------------------
    # REDUCE_POSITION
    # ------------------------------------------------------------------
    def _handle_reduce(
        self,
        event: ParsedTradeEvent,
        processing_order: int,
    ) -> HoldingsResult:
        result = self._resolve_best_existing(event)
        if result.status in {
            IdentityMatchStatus.AMBIGUOUS_MATCH,
            IdentityMatchStatus.IDENTITY_CONFLICT,
        }:
            return self._send_to_review(
                event,
                reason=self._identity_review_reason(result),
                processing_order=processing_order,
            )
        existing = result.matched_position

        if not existing or existing.status != "OPEN":
            return self._send_to_review(
                event,
                reason="MISSING_PRIOR_POSITION: no open position found to reduce.",
                processing_order=processing_order,
            )

        pos_before = self._clone(existing)
        pct = event.quantity_percent or Decimal("25")

        # Core formula: new = current * (1 - pct/100)
        new_alloc = existing.current_allocation_pct * (Decimal("1") - pct / Decimal("100"))

        # Quantise to 10 decimal places (avoids floating-point drift)
        new_alloc = new_alloc.quantize(Decimal("0.0000000001"), rounding=ROUND_DOWN)

        if new_alloc <= _ZERO_TOL:
            new_alloc = Decimal("0")
            existing.status = "CLOSED"

        existing.current_allocation_pct = new_alloc
        existing.updated_at = _now_iso()
        existing.version += 1

        pos = self._positions.save_position(existing)
        return self._finalise(event, pos_before, pos, processing_order, STATUS_APPLIED)

    # ------------------------------------------------------------------
    # CLOSE_POSITION
    # ------------------------------------------------------------------
    def _handle_close(
        self,
        event: ParsedTradeEvent,
        processing_order: int,
    ) -> HoldingsResult:
        result = self._resolve_best_existing(event)
        if result.status in {
            IdentityMatchStatus.AMBIGUOUS_MATCH,
            IdentityMatchStatus.IDENTITY_CONFLICT,
        }:
            return self._send_to_review(
                event,
                reason=self._identity_review_reason(result),
                processing_order=processing_order,
            )
        existing = result.matched_position

        if not existing or existing.status != "OPEN":
            return self._send_to_review(
                event,
                reason="MISSING_PRIOR_POSITION: no open position found to close.",
                processing_order=processing_order,
            )

        pos_before = self._clone(existing)
        existing.current_allocation_pct = Decimal("0")
        existing.status = "CLOSED"
        existing.updated_at = _now_iso()
        existing.version += 1

        pos = self._positions.save_position(existing)
        return self._finalise(event, pos_before, pos, processing_order, STATUS_APPLIED)

    # ------------------------------------------------------------------
    # HOLD_POSITION / TARGET_HIT / UPDATE_STOP_LOSS
    # ------------------------------------------------------------------
    def _handle_status_only(
        self,
        event: ParsedTradeEvent,
        action: str,
        processing_order: int,
    ) -> HoldingsResult:
        result = self._resolve_best_existing(event)
        if result.status in {
            IdentityMatchStatus.AMBIGUOUS_MATCH,
            IdentityMatchStatus.IDENTITY_CONFLICT,
        }:
            return self._send_to_review(
                event,
                reason=self._identity_review_reason(result),
                processing_order=processing_order,
            )
        existing = result.matched_position

        if not existing:
            # For status events, a missing position is a warning but not fatal
            # – we still record the event
            return self._send_to_review(
                event,
                reason="MISSING_PRIOR_POSITION: status event but no open position found.",
                processing_order=processing_order,
            )

        pos_before = self._clone(existing)

        if action == "UPDATE_STOP_LOSS" and event.stop_loss is not None:
            existing.stop_loss = event.stop_loss
            existing.updated_at = _now_iso()
            existing.version += 1
            pos = self._positions.save_position(existing)
        elif action == "HOLD_POSITION":
            # No change to position
            pos = existing
        else:
            # TARGET_HIT – no change
            pos = existing

        return self._finalise(event, pos_before, pos, processing_order, STATUS_STATUS_ONLY)

    # ------------------------------------------------------------------
    # Review routing
    # ------------------------------------------------------------------
    def _send_to_review(
        self,
        event: ParsedTradeEvent,
        reason: str,
        processing_order: int,
    ) -> HoldingsResult:
        msg_id = event.source_message_id or ""
        review_item = ReviewItem(
            source_message_id=msg_id,
            raw_text=event.raw_text,
            parsed_event_json=event.to_json(),
            review_reason=reason,
            review_status="PENDING",
            created_at=_now_iso(),
        )
        review_id = self._review.add_review_item(review_item)

        # Record in processed_messages so replay does not duplicate it
        if msg_id:
            self._processed.mark_processed(
                source_message_id=msg_id,
                text_hash="",
                processing_status=STATUS_MANUAL_REVIEW,
                trade_event_id=None,
            )

        return HoldingsResult(
            status="MANUAL_REVIEW",
            message=reason,
            review_item_id=review_id,
        )

    # ------------------------------------------------------------------
    # Finalise: persist event, snapshot, processed_message record
    # ------------------------------------------------------------------
    def _finalise(
        self,
        event: ParsedTradeEvent,
        pos_before: Optional[PositionState],
        pos_after: PositionState,
        processing_order: int,
        processing_status: str,
    ) -> HoldingsResult:
        msg_id = event.source_message_id or ""

        # Build immutable event record
        event_record = {
            "source_message_id": msg_id,
            "processing_order": processing_order,
            "raw_text": event.raw_text,
            "normalized_text": event.normalized_text,
            "clause_text": event.clause_text,
            "record_type": event.record_type,
            "final_action": event.final_action,
            "symbol": event.symbol,
            "direction": event.direction,
            "quantity_percent": str(event.quantity_percent) if event.quantity_percent else None,
            "quantity_basis": event.quantity_basis,
            "execution_prices_json": json.dumps(
                [str(p) for p in event.execution_prices]
            ),
            "stop_loss": str(event.stop_loss) if event.stop_loss else None,
            "targets_json": json.dumps([str(t) for t in event.targets]),
            "model_confidence": event.ml_confidence,
            "resolution_source": event.resolution_source,
            "position_before_json": json.dumps(_pos_to_dict(pos_before)),
            "position_after_json": json.dumps(_pos_to_dict(pos_after)),
            "processing_status": processing_status,
            "created_at": _now_iso(),
            "processed_at": _now_iso(),
            "parser_version": PARSER_VERSION,
            "parent_source_message_id": event.parent_source_message_id,
            "child_event_index": event.child_event_index,
        }
        event_id = self._events.insert_event(event_record)

        # Save processed_messages record
        if msg_id:
            self._processed.mark_processed(
                source_message_id=msg_id,
                text_hash="",
                processing_status=processing_status,
                trade_event_id=event_id,
            )

        # Snapshot
        open_positions = self._positions.list_open_positions(self._portfolio_id)
        snapshot_json = json.dumps(
            [_pos_to_dict(p) for p in open_positions],
            default=str,
        )
        self._snapshots.save_snapshot(
            trade_event_id=event_id,
            processing_order=processing_order,
            portfolio_id=self._portfolio_id,
            positions_json=snapshot_json,
        )

        return HoldingsResult(
            status="OK",
            message=f"Applied {event.final_action} for {event.symbol}.",
            position_before=pos_before,
            position_after=pos_after,
            trade_event_id=event_id,
        )

    # ------------------------------------------------------------------
    # Position lookup helpers
    # ------------------------------------------------------------------
    def _get_position(
        self,
        event: ParsedTradeEvent,
        direction: Optional[str],
    ) -> Optional[PositionState]:
        result = self._resolve_position(event, direction)
        if result.status in {
            IdentityMatchStatus.EXACT_MATCH,
            IdentityMatchStatus.UNIQUE_FALLBACK_MATCH,
        }:
            return result.matched_position
        return None

    def _resolve_position(
        self,
        event: ParsedTradeEvent,
        direction: Optional[str],
    ) -> IdentityResolutionResult:
        if hasattr(self._positions, "resolve_position"):
            return self._positions.resolve_position(
                portfolio_id=self._portfolio_id,
                symbol=event.symbol or "",
                direction=direction,
                market_group=event.market_group,
                contract_month=event.contract_month,
                option_type=event.option_type,
                strike_price=event.strike_price,
            )

        pos = self._positions.get_position(
            portfolio_id=self._portfolio_id,
            symbol=event.symbol or "",
            direction=direction,
            market_group=event.market_group,
            contract_month=event.contract_month,
            option_type=event.option_type,
            strike_price=event.strike_price,
        )
        if pos:
            return IdentityResolutionResult(
                status=IdentityMatchStatus.UNIQUE_FALLBACK_MATCH,
                matched_position=pos,
                candidate_count=1,
                candidate_position_ids=[pos.position_id] if pos.position_id else [],
                explanation="Legacy repository returned one position.",
            )
        return IdentityResolutionResult(
            status=IdentityMatchStatus.NO_MATCH,
            explanation="Legacy repository returned no position.",
        )

    def _find_closed_full_identity_position(
        self,
        event: ParsedTradeEvent,
        direction: str,
    ) -> Optional[PositionState]:
        target = identity_from_values(
            portfolio_id=self._portfolio_id,
            symbol=event.symbol or "",
            direction=direction,
            market_group=event.market_group,
            contract_month=event.contract_month,
            option_type=event.option_type,
            strike_price=event.strike_price,
        )
        for position in self._positions.list_all_positions(self._portfolio_id):
            if position.status != "CLOSED":
                continue
            candidate = identity_from_values(
                portfolio_id=position.portfolio_id,
                symbol=position.symbol,
                direction=position.direction,
                market_group=position.market_group,
                contract_month=position.contract_month,
                option_type=position.option_type,
                strike_price=position.strike_price,
            )
            if candidate.full_key() == target.full_key():
                return position
        return None

    def _resolve_best_existing(
        self,
        event: ParsedTradeEvent,
    ) -> IdentityResolutionResult:
        """
        Return the uniquely matching open position for an event.

        Direction may be omitted; in that case all supplied identity fields are
        applied and exactly one candidate must remain.
        """
        return self._resolve_position(event, event.direction)

    @staticmethod
    def _identity_review_reason(result: IdentityResolutionResult) -> str:
        if result.status == IdentityMatchStatus.AMBIGUOUS_MATCH:
            return (
                "AMBIGUOUS_POSITION_IDENTITY: multiple open positions match "
                f"candidate_ids={result.candidate_position_ids}; "
                f"fields_used={result.fields_used}; missing={result.fields_missing}."
            )
        if result.status == IdentityMatchStatus.IDENTITY_CONFLICT:
            return f"IDENTITY_CONFLICT: {result.explanation}"
        return f"MISSING_PRIOR_POSITION: {result.explanation}"

    @staticmethod
    def _clone(pos: PositionState) -> PositionState:
        """Return a shallow copy for the before-snapshot."""
        return PositionState(
            position_id=pos.position_id,
            portfolio_id=pos.portfolio_id,
            market_group=pos.market_group,
            symbol=pos.symbol,
            contract_month=pos.contract_month,
            option_type=pos.option_type,
            strike_price=pos.strike_price,
            direction=pos.direction,
            current_allocation_pct=pos.current_allocation_pct,
            average_entry_price=pos.average_entry_price,
            stop_loss=pos.stop_loss,
            targets=list(pos.targets),
            status=pos.status,
            opened_at=pos.opened_at,
            updated_at=pos.updated_at,
            version=pos.version,
        )
