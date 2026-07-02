"""
repository_interfaces.py
========================
Protocol (interface) definitions for all persistence repositories.

The holdings engine depends ONLY on these interfaces.
SQLite-specific implementations live in sqlite_repository.py.
Swapping to PostgreSQL or MongoDB later requires only a new implementation
of these protocols — no changes to the engine.

Python's typing.Protocol is used (structural subtyping).
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from src.schemas import HoldingsResult, ParsedTradeEvent, PositionState, ReviewItem
from src.position_identity import IdentityResolutionResult


@runtime_checkable
class PositionRepository(Protocol):
    """Read and write position state."""

    def get_position(
        self,
        portfolio_id: str,
        symbol: str,
        direction: Optional[str] = None,
        market_group: Optional[str] = None,
        contract_month: Optional[str] = None,
        option_type: Optional[str] = None,
        strike_price=None,
    ) -> Optional[PositionState]:
        """Return the current position or None if it does not exist."""
        ...

    def resolve_position(
        self,
        portfolio_id: str,
        symbol: str,
        direction: Optional[str] = None,
        market_group: Optional[str] = None,
        contract_month: Optional[str] = None,
        option_type: Optional[str] = None,
        strike_price=None,
    ) -> IdentityResolutionResult:
        """Return a typed exact/fallback/ambiguous identity resolution result."""
        ...

    def save_position(self, position: PositionState) -> PositionState:
        """Insert or update a position. Returns the saved state."""
        ...

    def list_open_positions(self, portfolio_id: str = "default") -> list[PositionState]:
        """Return all positions with status=OPEN."""
        ...

    def list_all_positions(self, portfolio_id: str = "default") -> list[PositionState]:
        """Return all positions regardless of status."""
        ...


@runtime_checkable
class TradeEventRepository(Protocol):
    """Immutable event ledger."""

    def insert_event(self, event_record: dict) -> int:
        """
        Insert an immutable trade event record.
        Returns the new record id.
        """
        ...

    def get_event(self, event_id: int) -> Optional[dict]:
        """Return one event record by id."""
        ...

    def list_events_for_symbol(self, symbol: str) -> list[dict]:
        """Return all events for a symbol, ordered by id."""
        ...

    def list_all_events(self) -> list[dict]:
        """Return all events ordered by id."""
        ...


@runtime_checkable
class ProcessedMessageRepository(Protocol):
    """Idempotency log."""

    def mark_processed(
        self,
        source_message_id: str,
        text_hash: str,
        processing_status: str,
        trade_event_id: Optional[int] = None,
    ) -> None:
        """Record that a message has been processed."""
        ...

    def is_processed(self, source_message_id: str) -> bool:
        """Return True if the message was already processed."""
        ...

    def get_status(self, source_message_id: str) -> Optional[str]:
        """Return the processing status for a message, or None."""
        ...


@runtime_checkable
class ReviewQueueRepository(Protocol):
    """Manual review queue."""

    def add_review_item(self, item: ReviewItem) -> int:
        """Add an item to the review queue. Returns the new id."""
        ...

    def list_pending(self) -> list[ReviewItem]:
        """Return all PENDING review items."""
        ...

    def list_all(self) -> list[ReviewItem]:
        """Return all review items."""
        ...

    def update_status(
        self,
        item_id: int,
        status: str,
        notes: Optional[str] = None,
    ) -> None:
        """Update the review status of an item."""
        ...


@runtime_checkable
class SnapshotRepository(Protocol):
    """Position snapshots after each applied event."""

    def save_snapshot(
        self,
        trade_event_id: int,
        processing_order: int,
        portfolio_id: str,
        positions_json: str,
    ) -> int:
        """Save a holdings snapshot. Returns the new id."""
        ...

    def list_snapshots(self, portfolio_id: str = "default") -> list[dict]:
        """Return all snapshots ordered by id."""
        ...
