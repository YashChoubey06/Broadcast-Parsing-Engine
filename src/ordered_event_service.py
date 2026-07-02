"""
Atomic application for ordered parsed message bundles.

The holdings engine remains responsible for one validated child event at a
time. This service coordinates the all-or-nothing parent operation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from src.holdings_engine import HoldingsEngine
from src.position_identity import (
    IdentityMatchStatus,
    identity_from_values,
)
from src.repository_interfaces import (
    PositionRepository,
    ProcessedMessageRepository,
    ReviewQueueRepository,
    SnapshotRepository,
    TradeEventRepository,
)
from src.schemas import HoldingsResult, ParsedMessageBundle, ParsedTradeEvent, ReviewItem


@dataclass
class OrderedApplyResult:
    status: str
    message: str = ""
    child_results: list[HoldingsResult] | None = None
    failed_child_index: Optional[int] = None
    review_reason: Optional[str] = None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    except Exception:
        return set()


class OrderedEventService:
    def __init__(
        self,
        *,
        conn: Optional[sqlite3.Connection],
        position_repo: PositionRepository,
        event_repo: TradeEventRepository,
        processed_repo: ProcessedMessageRepository,
        review_repo: ReviewQueueRepository,
        snapshot_repo: SnapshotRepository,
        portfolio_id: str,
        event_table_name: Optional[str] = None,
        parent_source_message_id: Optional[str] = None,
        mark_parent_processed: bool = False,
    ) -> None:
        self._conn = conn
        self._positions = position_repo
        self._event_repo = event_repo
        self._processed = processed_repo
        self._review = review_repo
        self._snapshots = snapshot_repo
        self._portfolio_id = portfolio_id
        self._event_table = event_table_name
        self._parent_source_message_id = parent_source_message_id
        self._mark_parent_processed = mark_parent_processed

    def apply_bundle(
        self,
        bundle: ParsedMessageBundle,
        processing_order: int = 0,
    ) -> OrderedApplyResult:
        parent_id = bundle.source_message_id or self._parent_source_message_id or ""
        if not bundle.is_ordered or bundle.bundle_type != "ORDERED_REVERSAL":
            return OrderedApplyResult("ERROR", "Bundle is not an ordered reversal.")
        if len(bundle.child_events) != 2:
            return self._review_result(bundle, "ORDERED_SPLIT_AMBIGUOUS")

        child1, child2 = bundle.child_events
        existing_state = self._existing_child_state(child1, child2)
        if existing_state == "COMPLETE":
            return OrderedApplyResult("ALREADY_PROCESSED", "Ordered parent was already processed.")
        if existing_state == "PARTIAL":
            return self._review_result(bundle, "PARTIAL_ORDERED_SEQUENCE_DETECTED")

        if parent_id and self._processed.is_processed(parent_id):
            return OrderedApplyResult("ALREADY_PROCESSED", "Ordered parent was already processed.")

        precheck = self._preflight(child1, child2)
        if precheck:
            return self._review_result(bundle, precheck)

        if self._conn is None:
            return self._apply_without_savepoint(bundle, processing_order, parent_id)

        self._conn.execute("SAVEPOINT ordered_reversal")
        try:
            result = self._apply_children(bundle, processing_order, parent_id)
            if result.status != "OK":
                self._conn.execute("ROLLBACK TO SAVEPOINT ordered_reversal")
                self._conn.execute("RELEASE SAVEPOINT ordered_reversal")
                self._add_parent_review(bundle, result.review_reason or result.message)
                return result
            self._conn.execute("RELEASE SAVEPOINT ordered_reversal")
            return result
        except Exception as exc:
            self._conn.execute("ROLLBACK TO SAVEPOINT ordered_reversal")
            self._conn.execute("RELEASE SAVEPOINT ordered_reversal")
            reason = f"ORDERED_CHILD_FAILED: {exc}"
            self._add_parent_review(bundle, reason)
            return OrderedApplyResult("MANUAL_REVIEW", reason, review_reason=reason)

    def _apply_without_savepoint(
        self,
        bundle: ParsedMessageBundle,
        processing_order: int,
        parent_id: str,
    ) -> OrderedApplyResult:
        return self._apply_children(bundle, processing_order, parent_id)

    def _apply_children(
        self,
        bundle: ParsedMessageBundle,
        processing_order: int,
        parent_id: str,
    ) -> OrderedApplyResult:
        engine = HoldingsEngine(
            position_repo=self._positions,
            event_repo=self._event_repo,
            processed_repo=self._processed,
            review_repo=self._review,
            snapshot_repo=self._snapshots,
            portfolio_id=self._portfolio_id,
        )

        child_results: list[HoldingsResult] = []
        child1, child2 = bundle.child_events
        if hasattr(self._event_repo, "current_parsed_json"):
            self._event_repo.current_parsed_json = child1.to_json()
        res1 = engine.apply_event(child1, processing_order=processing_order)
        child_results.append(res1)
        if res1.status != "OK":
            return OrderedApplyResult(
                "MANUAL_REVIEW",
                res1.message,
                child_results,
                failed_child_index=1,
                review_reason="ORDERED_CHILD_1_FAILED",
            )

        postcheck = self._post_child1_preflight(child2)
        if postcheck:
            return OrderedApplyResult(
                "MANUAL_REVIEW",
                postcheck,
                child_results,
                failed_child_index=2,
                review_reason=postcheck,
            )

        if hasattr(self._event_repo, "current_parsed_json"):
            self._event_repo.current_parsed_json = child2.to_json()
        res2 = engine.apply_event(child2, processing_order=processing_order + 1)
        child_results.append(res2)
        if res2.status != "OK":
            return OrderedApplyResult(
                "MANUAL_REVIEW",
                res2.message,
                child_results,
                failed_child_index=2,
                review_reason="ORDERED_CHILD_2_FAILED",
            )

        if self._mark_parent_processed and parent_id:
            self._processed.mark_processed(parent_id, "", "APPLIED", trade_event_id=None)
        return OrderedApplyResult("OK", "Applied ordered reversal.", child_results)

    def _preflight(self, child1: ParsedTradeEvent, child2: ParsedTradeEvent) -> Optional[str]:
        if child1.final_action != "CLOSE_POSITION":
            return "ORDERED_CHILD_1_NOT_FULL_CLOSE"
        if child2.final_action not in {"OPEN_LONG", "OPEN_SHORT", "ADD_LONG", "ADD_SHORT"}:
            return "ORDERED_CHILD_2_INVALID_ENTRY"
        if child1.direction not in {"LONG", "SHORT"}:
            return "ORDERED_CHILD_1_NO_POSITION"
        expected_new_side = "SHORT" if child1.direction == "LONG" else "LONG"
        if child2.direction != expected_new_side:
            return "ORDERED_NOT_OPPOSITE_DIRECTION"

        identity_reason = self._inherit_and_compare_identity(child1, child2)
        if identity_reason:
            return identity_reason

        old_result = self._positions.resolve_position(
            portfolio_id=self._portfolio_id,
            symbol=child1.symbol or "",
            direction=child1.direction,
            market_group=child1.market_group,
            contract_month=child1.contract_month,
            option_type=child1.option_type,
            strike_price=child1.strike_price,
        )
        if old_result.status == IdentityMatchStatus.NO_MATCH:
            return "ORDERED_CHILD_1_NO_POSITION"
        if old_result.status in {
            IdentityMatchStatus.AMBIGUOUS_MATCH,
            IdentityMatchStatus.IDENTITY_CONFLICT,
        }:
            return "ORDERED_CHILD_1_AMBIGUOUS_IDENTITY"

        opposite_result = self._positions.resolve_position(
            portfolio_id=self._portfolio_id,
            symbol=child2.symbol or "",
            direction=child2.direction,
            market_group=child2.market_group,
            contract_month=child2.contract_month,
            option_type=child2.option_type,
            strike_price=child2.strike_price,
        )
        if opposite_result.status in {
            IdentityMatchStatus.EXACT_MATCH,
            IdentityMatchStatus.UNIQUE_FALLBACK_MATCH,
        }:
            return "ORDERED_EXISTING_OPPOSITE_POSITION"
        if opposite_result.status in {
            IdentityMatchStatus.AMBIGUOUS_MATCH,
            IdentityMatchStatus.IDENTITY_CONFLICT,
        }:
            return "ORDERED_CHILD_1_AMBIGUOUS_IDENTITY"
        return None

    def _post_child1_preflight(self, child2: ParsedTradeEvent) -> Optional[str]:
        old_side = "SHORT" if child2.direction == "LONG" else "LONG"
        old_result = self._positions.resolve_position(
            portfolio_id=self._portfolio_id,
            symbol=child2.symbol or "",
            direction=old_side,
            market_group=child2.market_group,
            contract_month=child2.contract_month,
            option_type=child2.option_type,
            strike_price=child2.strike_price,
        )
        if old_result.status in {
            IdentityMatchStatus.EXACT_MATCH,
            IdentityMatchStatus.UNIQUE_FALLBACK_MATCH,
        }:
            return "ORDERED_CHILD_2_FAILED"
        return None

    def _inherit_and_compare_identity(
        self,
        child1: ParsedTradeEvent,
        child2: ParsedTradeEvent,
    ) -> Optional[str]:
        if not child1.symbol or not child2.symbol:
            return "ORDERED_SPLIT_AMBIGUOUS"
        id1 = identity_from_values(
            portfolio_id=self._portfolio_id,
            symbol=child1.symbol,
            market_group=child1.market_group,
            contract_month=child1.contract_month,
            option_type=child1.option_type,
            strike_price=child1.strike_price,
        )
        id2 = identity_from_values(
            portfolio_id=self._portfolio_id,
            symbol=child2.symbol,
            market_group=child2.market_group,
            contract_month=child2.contract_month,
            option_type=child2.option_type,
            strike_price=child2.strike_price,
        )
        if id1.symbol != id2.symbol:
            return "ORDERED_CHILD_IDENTITY_MISMATCH"

        for field_name in ("market_group", "contract_month", "option_type", "strike_price"):
            v1 = getattr(id1, field_name)
            v2 = getattr(id2, field_name)
            if field_name not in id2.supplied_fields and v1:
                setattr(child2, field_name, getattr(child1, field_name))
                continue
            if v1 != v2:
                return "ORDERED_CHILD_IDENTITY_MISMATCH"
        return None

    def _existing_child_state(
        self,
        child1: ParsedTradeEvent,
        child2: ParsedTradeEvent,
    ) -> str:
        ids = [child1.source_message_id, child2.source_message_id]
        processed = [bool(child_id and self._processed.is_processed(child_id)) for child_id in ids]
        event_rows = self._existing_event_rows(ids)
        seen = [processed[i] or event_rows[i] for i in range(2)]
        if all(seen):
            return "COMPLETE"
        if any(seen):
            return "PARTIAL"
        return "NONE"

    def _existing_event_rows(self, child_ids: list[Optional[str]]) -> list[bool]:
        if not self._conn or not self._event_table:
            return [False, False]
        cols = _table_columns(self._conn, self._event_table)
        result = []
        for child_id in child_ids:
            if not child_id:
                result.append(False)
                continue
            if "child_source_message_id" in cols:
                row = self._conn.execute(
                    f"SELECT 1 FROM {self._event_table} WHERE child_source_message_id=? LIMIT 1",
                    (child_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    f"SELECT 1 FROM {self._event_table} WHERE source_message_id=? LIMIT 1",
                    (child_id,),
                ).fetchone()
            result.append(row is not None)
        return result

    def _review_result(self, bundle: ParsedMessageBundle, reason: str) -> OrderedApplyResult:
        self._add_parent_review(bundle, reason)
        return OrderedApplyResult("MANUAL_REVIEW", reason, review_reason=reason)

    def _add_parent_review(self, bundle: ParsedMessageBundle, reason: str) -> None:
        source_id = bundle.source_message_id or self._parent_source_message_id
        if self._conn is not None:
            try:
                row = self._conn.execute(
                    """
                    SELECT id FROM manual_review_queue
                    WHERE source_message_id=? AND review_status='PENDING'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (source_id,),
                ).fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE manual_review_queue SET parsed_event_json=?, review_reason=? WHERE id=?",
                        (bundle.to_json(), reason, row["id"]),
                    )
                    return
            except Exception:
                pass
        self._review.add_review_item(
            ReviewItem(
                source_message_id=source_id,
                raw_text=bundle.raw_text,
                parsed_event_json=bundle.to_json(),
                review_reason=reason,
                review_status="PENDING",
            )
        )
