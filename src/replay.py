"""
replay.py
=========
Step 7: Historical replay of trade messages using local SQLite.

Reads 01_production_messages_final.csv in processing_order sequence,
passes each message through the parser (Step 5) and holdings engine (Step 6),
and persists results to SQLite.

Modes
-----
dry-run:
    Parses all messages, simulates holdings changes, writes reports only.
    Does NOT modify the real database.

safe-apply:
    Applies eligible messages to the real database.
    Sends ineligible messages to the manual-review queue.
    Records ALL messages as processed (idempotent on second run).

Usage
-----
    python -m src.replay \\
        --input data/01_production_messages_final.csv \\
        --db storage/trade_holdings.db \\
        --mode dry-run

    python -m src.replay \\
        --input data/01_production_messages_final.csv \\
        --db storage/trade_holdings.db \\
        --mode safe-apply

    python -m src.replay --reset-database --confirm-reset
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config import (
    DATABASE_PATH,
    DATA_DIR,
    DEFAULT_PORTFOLIO_ID,
    PRODUCTION_MESSAGES_FILE,
    REPORTS_DIR,
    STATUS_APPLIED,
    STATUS_CANCEL_PENDING,
    STATUS_CONDITIONAL,
    STATUS_MANUAL_REVIEW,
    STATUS_NON_TRADE,
    STATUS_STATUS_ONLY,
    STORAGE_DIR,
    resolve_input_path,
)
from src.database import drop_all_tables, get_connection, init_db
from src.holdings_engine import HoldingsEngine
from src.hybrid_parser import HybridParser
from src.schemas import ParsedTradeEvent
from src.sqlite_repository import (
    SQLitePositionRepository,
    SQLiteProcessedMessageRepository,
    SQLiteReviewQueueRepository,
    SQLiteSnapshotRepository,
    SQLiteTradeEventRepository,
)
from src.validator import validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_RECORD_TYPE_NON_TRADE = {"NON_TRADE"}
_RECORD_TYPE_STATUS = {"TRADE_UPDATE"}
_ALWAYS_REVIEW_ACTIONS = {
    "CORRECTION", "CANCEL_PREVIOUS", "CONDITIONAL_INSTRUCTION",
    "CLOSE_GROUP", "SPLIT_REQUIRED", "AMBIGUOUS",
}
_STATUS_ONLY_ACTIONS = {"HOLD_POSITION", "TARGET_HIT", "UPDATE_STOP_LOSS"}


# ---------------------------------------------------------------------------
# In-memory repository set for dry-run
# ---------------------------------------------------------------------------
import copy

class _InMemoryPositionRepo:
    def __init__(self):
        self._positions: dict = {}
        self._next_id = 1

    def get_position(self, portfolio_id, symbol, direction=None, **kwargs):
        matches = []
        for pos in self._positions.values():
            if (pos.portfolio_id == portfolio_id and pos.symbol == symbol
                    and (direction is None or pos.direction == direction)):
                matches.append(pos)
        if not matches:
            return None
        # Sort by position_id DESC and return first
        matches.sort(key=lambda p: getattr(p, 'position_id', 0) or 0, reverse=True)
        return copy.deepcopy(matches[0])

    def save_position(self, position):
        pos_copy = copy.deepcopy(position)
        if pos_copy.position_id is None:
            pos_copy.position_id = self._next_id
            position.position_id = self._next_id
            self._next_id += 1
        self._positions[pos_copy.position_id] = pos_copy
        return copy.deepcopy(pos_copy)

    def list_open_positions(self, portfolio_id="default"):
        return [copy.deepcopy(p) for p in self._positions.values()
                if p.portfolio_id == portfolio_id and p.status == "OPEN"]

    def list_all_positions(self, portfolio_id="default"):
        return [copy.deepcopy(p) for p in self._positions.values() if p.portfolio_id == portfolio_id]


class _InMemoryEventRepo:
    def __init__(self):
        self._events: list = []
        self._next_id = 1

    def insert_event(self, record):
        record["id"] = self._next_id
        self._events.append(record)
        self._next_id += 1
        return record["id"]

    def get_event(self, event_id):
        for e in self._events:
            if e.get("id") == event_id:
                return e
        return None

    def list_events_for_symbol(self, symbol):
        return [e for e in self._events if e.get("symbol") == symbol]

    def list_all_events(self):
        return list(self._events)


class _InMemoryProcessedRepo:
    def __init__(self):
        self._seen: dict = {}

    def mark_processed(self, source_message_id, text_hash, processing_status, trade_event_id=None):
        self._seen[source_message_id] = processing_status

    def is_processed(self, source_message_id):
        return source_message_id in self._seen

    def get_status(self, source_message_id):
        return self._seen.get(source_message_id)


class _InMemoryReviewRepo:
    def __init__(self):
        self._items: list = []
        self._next_id = 1

    def add_review_item(self, item):
        item.id = self._next_id
        self._items.append(item)
        self._next_id += 1
        return item.id

    def list_pending(self):
        return [i for i in self._items if i.review_status == "PENDING"]

    def list_all(self):
        return list(self._items)

    def update_status(self, item_id, status, notes=None):
        for item in self._items:
            if item.id == item_id:
                item.review_status = status
                item.resolution_notes = notes
                break


class _InMemorySnapshotRepo:
    def __init__(self):
        self._snaps: list = []
        self._next_id = 1

    def save_snapshot(self, trade_event_id, processing_order, portfolio_id, positions_json):
        snap = {
            "id": self._next_id,
            "trade_event_id": trade_event_id,
            "processing_order": processing_order,
            "portfolio_id": portfolio_id,
            "positions_json": positions_json,
        }
        self._snaps.append(snap)
        self._next_id += 1
        return snap["id"]

    def list_snapshots(self, portfolio_id="default"):
        return [s for s in self._snaps if s["portfolio_id"] == portfolio_id]


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------
class ReplayStats:
    def __init__(self):
        self.total_read = 0
        self.parsed = 0
        self.applied = 0
        self.non_trade_skipped = 0
        self.status_only = 0
        self.duplicates_ignored = 0
        self.sent_to_review = 0
        self.failed = 0
        self.disagreements: list[dict] = []
        self.review_items: list[dict] = []
        self.errors: list[dict] = []

    def to_dict(self) -> dict:
        return {
            "total_read": self.total_read,
            "parsed": self.parsed,
            "applied": self.applied,
            "non_trade_skipped": self.non_trade_skipped,
            "status_only": self.status_only,
            "duplicates_ignored": self.duplicates_ignored,
            "sent_to_review": self.sent_to_review,
            "failed": self.failed,
            "disagreement_count": len(self.disagreements),
        }


# ---------------------------------------------------------------------------
# Core replay function
# ---------------------------------------------------------------------------
def _determine_processing_status(event: ParsedTradeEvent) -> str:
    """Determine the processing status code for a parsed event."""
    action = event.final_action
    if event.record_type in _RECORD_TYPE_NON_TRADE or action in ("NON_TRADE", "ADMIN_NOTICE", "UNKNOWN"):
        return STATUS_NON_TRADE
    if action == "CONDITIONAL_INSTRUCTION":
        return STATUS_CONDITIONAL
    if action == "CANCEL_PREVIOUS":
        return STATUS_CANCEL_PENDING
    if action in _STATUS_ONLY_ACTIONS:
        return STATUS_STATUS_ONLY
    if action in _ALWAYS_REVIEW_ACTIONS or event.needs_review:
        return STATUS_MANUAL_REVIEW
    return STATUS_APPLIED


def run_replay(
    input_path: Path,
    db_path: Path,
    mode: str = "dry-run",
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
    verbose: bool = False,
) -> ReplayStats:
    """
    Execute the historical replay.

    mode='dry-run':  in-memory only; does not touch the database.
    mode='safe-apply': writes to the real database.
    """
    # ---- Load production messages ----------------------------------------
    print(f"\nLoading messages from: {input_path}")
    df = pd.read_csv(input_path, low_memory=False)

    # Sort by processing_order (primary), created_at (secondary)
    sort_cols = []
    if "processing_order" in df.columns:
        sort_cols.append("processing_order")
    if "created_at" in df.columns:
        sort_cols.append("created_at")
    if sort_cols:
        df = df.sort_values(sort_cols).reset_index(drop=True)

    stats = ReplayStats()
    stats.total_read = len(df)
    print(f"Messages to process: {stats.total_read}")
    print(f"Mode: {mode.upper()}\n")

    # ---- Set up repositories ----------------------------------------------
    if mode == "dry-run":
        pos_repo = _InMemoryPositionRepo()
        event_repo = _InMemoryEventRepo()
        processed_repo = _InMemoryProcessedRepo()
        review_repo = _InMemoryReviewRepo()
        snapshot_repo = _InMemorySnapshotRepo()
        conn = None
    else:
        init_db(db_path)
        conn = get_connection(db_path)
        pos_repo = SQLitePositionRepository(conn)
        event_repo = SQLiteTradeEventRepository(conn)
        processed_repo = SQLiteProcessedMessageRepository(conn)
        review_repo = SQLiteReviewQueueRepository(conn)
        snapshot_repo = SQLiteSnapshotRepository(conn)

    parser = HybridParser()
    engine = HoldingsEngine(
        position_repo=pos_repo,
        event_repo=event_repo,
        processed_repo=processed_repo,
        review_repo=review_repo,
        snapshot_repo=snapshot_repo,
        portfolio_id=portfolio_id,
    )

    # ---- Process each message --------------------------------------------
    for _, row in df.iterrows():
        msg_id = str(row.get("source_message_id", "")) or None
        raw_text = str(row.get("raw_text", "")).strip()
        processing_order = int(row.get("processing_order", 0))
        market_group = str(row.get("market_group", "")) or None

        # Dataset-supplied labels (for disagreement detection)
        ds_action = str(row.get("action_label", "")).strip()
        ds_symbol = str(row.get("symbol", "")).strip()
        ds_auto_apply = str(row.get("auto_apply_eligible", "")).lower() in ("true", "1", "yes")
        ds_needs_review = str(row.get("needs_review", "")).lower() in ("true", "1", "yes")
        ds_requires_context = str(row.get("requires_context", "")).lower() in ("true", "1", "yes")

        if not raw_text:
            continue

        # ---- Duplicate check -----------------------------------------------
        if msg_id and processed_repo.is_processed(msg_id):
            stats.duplicates_ignored += 1
            if verbose:
                print(f"  SKIP (dup) {msg_id}: {raw_text[:60]}")
            continue

        # ---- Holdings context for SELL resolution --------------------------
        existing_long = False
        existing_short = False

        # ---- Parse ---------------------------------------------------------
        try:
            # Pre-parse without context to get symbol
            pre_event = parser.parse(raw_text, market_group=market_group)
            sym = pre_event.symbol

            if sym:
                lp = pos_repo.get_position(portfolio_id, sym, direction="LONG")
                sp = pos_repo.get_position(portfolio_id, sym, direction="SHORT")
                existing_long = bool(lp and lp.status == "OPEN")
                existing_short = bool(sp and sp.status == "OPEN")

            event = parser.parse(
                raw_text=raw_text,
                source_message_id=msg_id,
                existing_long=existing_long,
                existing_short=existing_short,
                has_holdings_context=True,
                market_group=market_group,
            )
            stats.parsed += 1
        except Exception as exc:
            stats.failed += 1
            stats.errors.append({
                "source_message_id": msg_id,
                "raw_text": raw_text[:120],
                "error": str(exc),
            })
            continue

        # ---- Disagreeement detection ----------------------------------------
        if ds_action and event.final_action and ds_action != event.final_action:
            stats.disagreements.append({
                "source_message_id": msg_id,
                "raw_text": raw_text[:120],
                "dataset_action": ds_action,
                "ml_action": event.ml_action or "",
                "rule_action": event.rule_action or "",
                "parser_action": event.final_action,
                "confidence": event.ml_confidence or 0.0,
                "resolution_source": event.resolution_source,
                "dataset_symbol": ds_symbol,
                "parser_symbol": event.symbol or "",
                "reason": "DATASET_PARSER_DISAGREEMENT",
            })

        # ---- Validate -------------------------------------------------------
        already_processed = bool(msg_id and processed_repo.is_processed(msg_id))
        event = validate(event, confidence=event.ml_confidence, already_processed=already_processed)

        # ---- Apply safe-apply criteria ------------------------------------
        # For safe-apply: dataset flags AND fresh parser must both agree
        ds_eligible = (
            ds_auto_apply and
            not ds_needs_review and
            not ds_requires_context
        )

        # Parser disagreement → do not auto-apply
        parser_disagrees = (ds_action and event.final_action and ds_action != event.final_action)

        can_apply = (
            event.auto_apply_eligible and
            not event.needs_review and
            not event.is_multi_instrument and
            not event.requires_context and
            not parser_disagrees and
            ds_eligible and
            msg_id  # must have an ID for idempotency
        )

        # Non-trade messages: mark processed and skip
        if event.record_type == "NON_TRADE" or event.final_action in ("NON_TRADE", "ADMIN_NOTICE", "UNKNOWN"):
            stats.non_trade_skipped += 1
            if msg_id and mode == "safe-apply":
                try:
                    processed_repo.mark_processed(msg_id, "", STATUS_NON_TRADE)
                    if conn:
                        conn.commit()
                except Exception:
                    if conn:
                        conn.rollback()
            elif msg_id and mode == "dry-run":
                processed_repo.mark_processed(msg_id, "", STATUS_NON_TRADE)
            continue

        # ---- Apply or review ---------------------------------------------
        if can_apply:
            try:
                result = engine.apply_event(event, processing_order=processing_order)
                if conn:
                    conn.commit()

                if result.status == "ALREADY_PROCESSED":
                    stats.duplicates_ignored += 1
                elif result.status in ("OK",):
                    if event.final_action in ("HOLD_POSITION", "TARGET_HIT", "UPDATE_STOP_LOSS"):
                        stats.status_only += 1
                    else:
                        stats.applied += 1
                elif result.status == "MANUAL_REVIEW":
                    stats.sent_to_review += 1
                    stats.review_items.append({
                        "source_message_id": msg_id,
                        "raw_text": raw_text[:120],
                        "reason": result.message,
                    })

            except Exception as exc:
                if conn:
                    conn.rollback()
                stats.failed += 1
                stats.errors.append({
                    "source_message_id": msg_id,
                    "raw_text": raw_text[:120],
                    "error": str(exc),
                })
        else:
            # Send to review
            reason = _review_reason(event, parser_disagrees, ds_eligible, mode)
            stats.sent_to_review += 1
            stats.review_items.append({
                "source_message_id": msg_id,
                "raw_text": raw_text[:120],
                "reason": reason,
                "final_action": event.final_action,
            })

            # Still mark as processed so second run does not duplicate
            if msg_id:
                proc_status = _determine_processing_status(event)
                if parser_disagrees:
                    proc_status = STATUS_MANUAL_REVIEW

                try:
                    if mode == "safe-apply":
                        from src.schemas import ReviewItem
                        review_item_obj = ReviewItem(
                            source_message_id=msg_id,
                            raw_text=raw_text[:500],
                            parsed_event_json=event.to_json(),
                            review_reason=reason,
                            review_status="PENDING",
                        )
                        review_repo.add_review_item(review_item_obj)
                        processed_repo.mark_processed(msg_id, "", proc_status)
                        if conn:
                            conn.commit()
                    else:
                        # Dry-run: mark in-memory
                        from src.schemas import ReviewItem
                        review_item_obj = ReviewItem(
                            source_message_id=msg_id,
                            raw_text=raw_text[:500],
                            parsed_event_json=event.to_json(),
                            review_reason=reason,
                            review_status="PENDING",
                        )
                        review_repo.add_review_item(review_item_obj)
                        processed_repo.mark_processed(msg_id, "", proc_status)
                except Exception as exc2:
                    if conn:
                        conn.rollback()
                    stats.failed += 1

        if verbose:
            sym_disp = event.symbol or "?"
            print(
                f"  [{processing_order:>4}] {event.final_action:<25} {sym_disp:<20} "
                f"{'APPLY' if can_apply else 'REVIEW'}"
            )

    # Connection is intentionally left open so _save_reports can query the repos.
    # It will be closed on process exit.

    return stats, pos_repo, review_repo, event_repo


def _review_reason(event, parser_disagrees, ds_eligible, mode) -> str:
    if parser_disagrees:
        return "DATASET_PARSER_DISAGREEMENT"
    if event.is_multi_instrument:
        return "MULTI_INSTRUMENT"
    if event.is_correction:
        return "CORRECTION_MESSAGE"
    if event.is_conditional:
        return "CONDITIONAL_INSTRUCTION"
    if event.final_action == "CANCEL_PREVIOUS":
        return "CANCEL_PREVIOUS"
    if event.final_action == "CLOSE_GROUP":
        return "GROUP_ACTION"
    if event.requires_context:
        return "REQUIRES_CONTEXT"
    if not event.symbol:
        return "MISSING_SYMBOL"
    if event.needs_review:
        return "VALIDATION_FAILED_OR_LOW_CONFIDENCE"
    if not ds_eligible and mode == "safe-apply":
        return "DATASET_NOT_AUTO_APPLY_ELIGIBLE"
    return "NOT_AUTO_APPLY_ELIGIBLE"


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def _save_reports(stats: ReplayStats, pos_repo, review_repo, event_repo, mode: str) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    prefix = "replay_dry_run" if mode == "dry-run" else "final"

    # Summary JSON
    summary = stats.to_dict()
    with open(REPORTS_DIR / f"{prefix}_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    # Final holdings CSV
    positions = pos_repo.list_all_positions()
    if positions:
        rows = []
        for p in positions:
            rows.append({
                "symbol": p.symbol,
                "direction": p.direction,
                "status": p.status,
                "current_allocation_pct": str(p.current_allocation_pct),
                "average_entry_price": str(p.average_entry_price) if p.average_entry_price else "",
                "stop_loss": str(p.stop_loss) if p.stop_loss else "",
                "market_group": p.market_group or "",
            })
        pd.DataFrame(rows).to_csv(
            REPORTS_DIR / f"{'replay_dry_run_final_holdings' if mode == 'dry-run' else 'final_holdings'}.csv",
            index=False,
        )

    # Review queue CSV
    review_items = review_repo.list_all()
    if review_items:
        rrows = []
        for ri in review_items:
            rrows.append({
                "source_message_id": ri.source_message_id,
                "raw_text": ri.raw_text[:100],
                "review_reason": ri.review_reason,
                "review_status": ri.review_status,
            })
        pd.DataFrame(rrows).to_csv(
            REPORTS_DIR / f"{'replay_dry_run_review_queue' if mode == 'dry-run' else 'manual_review_queue'}.csv",
            index=False,
        )

    # Disagreements CSV
    if stats.disagreements:
        pd.DataFrame(stats.disagreements).to_csv(
            REPORTS_DIR / "dataset_parser_disagreements.csv", index=False
        )

    # Errors CSV
    if stats.errors:
        pd.DataFrame(stats.errors).to_csv(
            REPORTS_DIR / "replay_errors.csv", index=False
        )

    # Event ledger CSV
    try:
        events = event_repo.list_all_events()
        if events:
            pd.DataFrame(events).to_csv(
                REPORTS_DIR / "trade_event_ledger.csv", index=False
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Historical replay of trade messages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input", default=str(DATA_DIR / PRODUCTION_MESSAGES_FILE),
        help="Path to the production messages CSV."
    )
    parser.add_argument(
        "--db", default=str(DATABASE_PATH),
        help="SQLite database path."
    )
    parser.add_argument(
        "--mode", choices=["dry-run", "safe-apply"], default="dry-run",
        help="Replay mode: dry-run (no DB changes) or safe-apply."
    )
    parser.add_argument(
        "--portfolio", default=DEFAULT_PORTFOLIO_ID,
        help="Portfolio ID."
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print each message result."
    )
    parser.add_argument(
        "--reset-database", action="store_true",
        help="[DEV ONLY] Drop and recreate all tables."
    )
    parser.add_argument(
        "--confirm-reset", action="store_true",
        help="Required with --reset-database."
    )
    args = parser.parse_args()

    if args.reset_database:
        db_path = Path(args.db).resolve()
        # Safety: never delete outside storage/
        storage_resolved = STORAGE_DIR.resolve()
        if not str(db_path).startswith(str(storage_resolved)):
            print(f"ERROR: Database {db_path} is outside the configured storage/ directory.")
            print("Reset aborted for safety.")
            return
        if not args.confirm_reset:
            print("ERROR: --reset-database requires --confirm-reset.")
            print("This will DROP ALL TABLES. Add --confirm-reset to proceed.")
            return
        print(f"Resetting database: {db_path}")
        drop_all_tables(db_path)
        init_db(db_path)
        print("Database reset complete.")
        return

    try:
        input_path = resolve_input_path(args.input)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return

    stats, pos_repo, review_repo, event_repo = run_replay(
        input_path=input_path,
        db_path=Path(args.db),
        mode=args.mode,
        portfolio_id=args.portfolio,
        verbose=args.verbose,
    )

    _save_reports(stats, pos_repo, review_repo, event_repo, args.mode)

    # ---- Print summary ---------------------------------------------------
    print(f"\n{'='*60}")
    print(f"REPLAY SUMMARY ({args.mode.upper()})")
    print(f"{'='*60}")
    print(f"  Messages read:          {stats.total_read}")
    print(f"  Messages parsed:        {stats.parsed}")
    print(f"  Messages applied:       {stats.applied}")
    print(f"  Non-trade skipped:      {stats.non_trade_skipped}")
    print(f"  Status-only events:     {stats.status_only}")
    print(f"  Duplicates ignored:     {stats.duplicates_ignored}")
    print(f"  Sent to review:         {stats.sent_to_review}")
    print(f"  Failed:                 {stats.failed}")
    print(f"  Parser/dataset disagr.: {len(stats.disagreements)}")

    # Final open positions
    open_positions = [p for p in pos_repo.list_all_positions() if p.status == "OPEN"]
    closed_positions = [p for p in pos_repo.list_all_positions() if p.status == "CLOSED"]
    print(f"\n  Open positions:  {len(open_positions)}")
    print(f"  Closed positions: {len(closed_positions)}")

    if open_positions:
        print(f"\n  Reconstructable open positions (from supplied historical period):")
        for p in open_positions:
            print(
                f"    {p.symbol:<20} {p.direction or '?':<6} "
                f"{p.current_allocation_pct}% [{p.market_group or '?'}]"
            )

    print(f"\nReports saved to: {REPORTS_DIR}/")
    print("NOTE: Holdings reflect positions reconstructable from the supplied")
    print("      historical period only. They may not represent the complete portfolio.")


if __name__ == "__main__":
    main()
