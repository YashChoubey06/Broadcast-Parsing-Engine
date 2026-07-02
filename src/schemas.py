"""
schemas.py
==========
All typed dataclasses used throughout the system.

ParsedTradeEvent  — output of Step 5 (the NLP/rule parser).
PositionState     — a single position in the holdings engine.
HoldingsResult    — return value from the holdings engine.
ReviewItem        — a manual-review record.

IMPORTANT: The NLP model must never modify holdings directly.
           Only ParsedTradeEvent objects, after passing validation,
           may be passed to the holdings engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Optional


# ---------------------------------------------------------------------------
# Helper: Decimal-aware JSON encoder
# ---------------------------------------------------------------------------
class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


def _to_json(obj) -> str:
    return json.dumps(obj, cls=_DecimalEncoder, ensure_ascii=False)


# ---------------------------------------------------------------------------
# ParsedTradeEvent
# ---------------------------------------------------------------------------
@dataclass
class ParsedTradeEvent:
    """
    Structured output produced by the hybrid NLP parser (Step 5).

    This object is the ONLY thing that may be passed to the holdings engine.
    It carries every field needed for deterministic position management.
    """

    # ---- Identity ----------------------------------------------------------
    source_message_id: Optional[str] = None
    raw_text: str = ""
    normalized_text: str = ""
    clause_text: Optional[str] = None

    # ---- Classification ----------------------------------------------------
    record_type: str = "UNKNOWN"           # TRADE_ACTION | TRADE_UPDATE | TRADE_CONTROL | NON_TRADE
    ml_action: Optional[str] = None
    ml_confidence: Optional[float] = None
    rule_action: Optional[str] = None
    final_action: str = "UNKNOWN"
    resolution_source: str = "MANUAL_REVIEW"  # RULE | ML | RULE_AND_ML_AGREE | CONTEXT_RESOLVED | MANUAL_REVIEW

    # ---- Confirmed semantics (v2) -----------------------------------------
    semantics_version: str = "v2"
    surface_instruction: Optional[str] = None
    entry_capacity_pct: Optional[Decimal] = None
    reduction_pct: Optional[Decimal] = None
    position_effect: Optional[str] = None
    resolved_position_side: Optional[str] = None

    # ---- Instrument --------------------------------------------------------
    symbol_raw: Optional[str] = None
    symbol: Optional[str] = None
    symbols: list[str] = field(default_factory=list)
    market_group: Optional[str] = None
    contract_month: Optional[str] = None
    strike_price: Optional[Decimal] = None
    option_type: Optional[str] = None     # CE | PE | CALL | PUT

    # ---- Direction & Quantity ----------------------------------------------
    direction: Optional[str] = None       # LONG | SHORT
    quantity_percent: Optional[Decimal] = None
    quantity_basis: str = "UNKNOWN"       # CURRENT_HOLDING | MODEL_ALLOCATION | NONE | NOT_APPLICABLE
    remaining_holding_multiplier: Optional[Decimal] = None

    # ---- Prices ------------------------------------------------------------
    execution_prices: list[Decimal] = field(default_factory=list)
    execution_price_primary: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    targets: list[Decimal] = field(default_factory=list)

    # ---- Flags -------------------------------------------------------------
    is_correction: bool = False
    is_conditional: bool = False
    is_multi_instrument: bool = False
    requires_context: bool = False

    # ---- Multi-instrument child tracking -----------------------------------
    parent_source_message_id: Optional[str] = None
    child_event_index: Optional[int] = None

    # ---- Correction / cancel linkage (schema reserved for future linking) --
    related_source_message_id: Optional[str] = None
    supersedes_event_id: Optional[str] = None
    cancels_event_id: Optional[str] = None

    # ---- Validation --------------------------------------------------------
    auto_apply_eligible: bool = False
    needs_review: bool = False
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict (Decimal → str)."""
        d = {}
        for k, v in asdict(self).items():
            if isinstance(v, Decimal):
                d[k] = str(v)
            elif isinstance(v, list):
                d[k] = [str(i) if isinstance(i, Decimal) else i for i in v]
            else:
                d[k] = v
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "ParsedTradeEvent":
        d = json.loads(json_str)
        for field_name in (
            "strike_price",
            "quantity_percent",
            "entry_capacity_pct",
            "reduction_pct",
            "stop_loss",
        ):
            if d.get(field_name) is not None:
                d[field_name] = Decimal(str(d[field_name]))

        if d.get("execution_prices") is not None:
            d["execution_prices"] = [Decimal(str(x)) for x in d["execution_prices"]]
        if d.get("entry_prices") is not None:
            d["execution_prices"] = [Decimal(str(x)) for x in d.pop("entry_prices")]
        if d.get("targets") is not None:
            d["targets"] = [Decimal(str(x)) for x in d["targets"]]

        if d.get("quantity_basis") == "MODEL_ALLOCATION":
            d["quantity_basis"] = "CUSTOMER_BUYING_CAPACITY"
        elif d.get("quantity_basis") == "CURRENT_HOLDING":
            d["quantity_basis"] = "CURRENT_POSITION"

        field_names = set(cls.__dataclass_fields__)
        d = {k: v for k, v in d.items() if k in field_names}
        return cls(**d)


# ---------------------------------------------------------------------------
# PositionState
# ---------------------------------------------------------------------------
@dataclass
class PositionState:
    """
    A single open or closed position.

    Allocation values are stored as Decimal for exact arithmetic.
    Status: OPEN | CLOSED | REVIEW
    """

    position_id: Optional[int] = None
    portfolio_id: str = "default"
    market_group: Optional[str] = None
    symbol: str = ""
    contract_month: Optional[str] = None
    option_type: Optional[str] = None
    strike_price: Optional[Decimal] = None
    direction: Optional[str] = None       # LONG | SHORT

    current_allocation_pct: Decimal = Decimal("0")
    average_entry_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    targets: list[Decimal] = field(default_factory=list)

    status: str = "OPEN"                  # OPEN | CLOSED | REVIEW
    opened_at: Optional[str] = None
    updated_at: Optional[str] = None
    version: int = 1

    def position_key(self) -> tuple:
        """Unique key used to look up this position."""
        return (
            self.portfolio_id,
            self.market_group or "",
            self.symbol,
            self.contract_month or "",
            self.option_type or "",
            str(self.strike_price) if self.strike_price else "",
            self.direction or "",
        )


# ---------------------------------------------------------------------------
# HoldingsResult
# ---------------------------------------------------------------------------
@dataclass
class HoldingsResult:
    """
    Return value from holdings_engine.apply_event().
    """
    status: str = "OK"           # OK | ALREADY_PROCESSED | MANUAL_REVIEW | ERROR
    message: str = ""
    position_before: Optional[PositionState] = None
    position_after: Optional[PositionState] = None
    trade_event_id: Optional[int] = None
    review_item_id: Optional[int] = None


# ---------------------------------------------------------------------------
# ReviewItem
# ---------------------------------------------------------------------------
@dataclass
class ReviewItem:
    """
    A record in the manual review queue.
    """
    id: Optional[int] = None
    source_message_id: Optional[str] = None
    raw_text: str = ""
    parsed_event_json: str = ""
    review_reason: str = ""
    review_status: str = "PENDING"         # PENDING | APPROVED | REJECTED | CORRECTED
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None
    resolution_notes: Optional[str] = None


# ---------------------------------------------------------------------------
# ParsedMessageBundle
# ---------------------------------------------------------------------------
@dataclass
class ParsedMessageBundle:
    """
    Message-level parser output.

    Single-clause messages are represented as a one-child bundle so existing
    event-level APIs can keep working while ordered messages preserve parent
    and child semantics explicitly.
    """

    source_message_id: Optional[str] = None
    raw_text: str = ""
    normalized_text: str = ""
    is_ordered: bool = False
    bundle_type: str = "SINGLE"
    child_events: list[ParsedTradeEvent] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    auto_apply_eligible: bool = False
    needs_review: bool = False

    def to_dict(self) -> dict:
        return {
            "source_message_id": self.source_message_id,
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "is_ordered": self.is_ordered,
            "bundle_type": self.bundle_type,
            "child_events": [child.to_dict() for child in self.child_events],
            "validation_errors": list(self.validation_errors),
            "validation_warnings": list(self.validation_warnings),
            "auto_apply_eligible": self.auto_apply_eligible,
            "needs_review": self.needs_review,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "ParsedMessageBundle":
        d = json.loads(json_str)
        if "child_events" not in d:
            event = ParsedTradeEvent.from_json(json_str)
            return cls(
                source_message_id=event.source_message_id,
                raw_text=event.raw_text,
                normalized_text=event.normalized_text,
                child_events=[event],
                auto_apply_eligible=event.auto_apply_eligible,
                needs_review=event.needs_review,
            )

        children = [
            ParsedTradeEvent.from_json(json.dumps(child, ensure_ascii=False))
            for child in d.get("child_events", [])
        ]
        return cls(
            source_message_id=d.get("source_message_id"),
            raw_text=d.get("raw_text", ""),
            normalized_text=d.get("normalized_text", ""),
            is_ordered=bool(d.get("is_ordered", False)),
            bundle_type=d.get("bundle_type", "SINGLE"),
            child_events=children,
            validation_errors=list(d.get("validation_errors", [])),
            validation_warnings=list(d.get("validation_warnings", [])),
            auto_apply_eligible=bool(d.get("auto_apply_eligible", False)),
            needs_review=bool(d.get("needs_review", False)),
        )
