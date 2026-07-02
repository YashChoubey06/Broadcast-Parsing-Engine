import json
from datetime import datetime, timezone
import sqlite3
from typing import Optional, List, Dict, Any
from pathlib import Path

from src.database import get_connection, transaction
from src.config import DATABASE_PATH
from src.schemas import ParsedTradeEvent
from src.schemas import ParsedMessageBundle
from src.hybrid_parser import HybridParser
from src.validator import validate
from src.holdings_engine import HoldingsEngine, HoldingsResult
from src.ordered_event_service import OrderedEventService
from src.sqlite_repository import SQLitePositionRepository
from src.position_identity import IdentityMatchStatus
from src.repository_interfaces import TradeEventRepository, ProcessedMessageRepository, ReviewQueueRepository, SnapshotRepository

class DynamicEventRepoAdapter(TradeEventRepository):
    def __init__(self, conn: sqlite3.Connection, table_name: str):
        self.conn = conn
        self.table_name = table_name
        self.current_parsed_json = None
        self.current_review_id = None
        self.last_inserted_id = None

    def insert_event(self, event_record: dict) -> int:
        pos_before = event_record.get('position_before_json')
        pos_after = event_record.get('position_after_json')
        status = event_record.get('processing_status')
        msg_id = event_record.get('source_message_id')
        now = datetime.now(timezone.utc).isoformat()

        child_msg_id = event_record.get('source_message_id')
        parent_msg_id = event_record.get('parent_source_message_id') or msg_id
        child_index = event_record.get('child_event_index')
        clause_text = None
        try:
            payload = json.loads(self.current_parsed_json or "{}")
            clause_text = payload.get("clause_text")
        except Exception:
            clause_text = None

        if self.table_name == "shadow_events":
            cur = _insert_shadow_event(
                self.conn, parent_msg_id, child_msg_id, child_index, clause_text,
                self.current_parsed_json, pos_before, pos_after, status, now
            )
        else:
            cur = _insert_verified_event(
                self.conn, parent_msg_id, child_msg_id, child_index, clause_text,
                self.current_parsed_json, pos_before, pos_after, self.current_review_id, now
            )
        self.last_inserted_id = cur.lastrowid
        return cur.lastrowid

    def get_event(self, event_id: int): return None
    def list_events_for_symbol(self, symbol: str): return []
    def list_all_events(self): return []

class NoOpProcessedRepo(ProcessedMessageRepository):
    def mark_processed(self, source_message_id, text_hash, processing_status, trade_event_id=None): pass
    def is_processed(self, source_message_id): return False
    def get_processing_status(self, source_message_id): return None

class NoOpReviewRepo(ReviewQueueRepository):
    def add_review_item(self, record): return 0
    def list_pending(self): return []
    def resolve_item(self, review_id, status, notes=""): pass
    def get_item(self, review_id): return None

class NoOpSnapshotRepo(SnapshotRepository):
    def save_snapshot(self, trade_event_id, processing_order, portfolio_id, positions_json): pass
    def get_snapshot(self, trade_event_id, processing_order, portfolio_id): return None

def _get_engine(conn: sqlite3.Connection, portfolio_id: str, event_json: str, review_id: int = None) -> HoldingsEngine:
    pos_repo = SQLitePositionRepository(conn)
    table = "shadow_events" if portfolio_id == "shadow" else "verified_events"
    event_repo = DynamicEventRepoAdapter(conn, table)
    event_repo.current_parsed_json = event_json
    event_repo.current_review_id = review_id
    
    return HoldingsEngine(
        position_repo=pos_repo,
        event_repo=event_repo,
        processed_repo=NoOpProcessedRepo(),
        review_repo=NoOpReviewRepo(),
        snapshot_repo=NoOpSnapshotRepo(),
        portfolio_id=portfolio_id
    )


def _get_ordered_service(
    conn: sqlite3.Connection,
    portfolio_id: str,
    review_id: int = None,
    parent_source_message_id: str = None,
) -> OrderedEventService:
    pos_repo = SQLitePositionRepository(conn)
    table = "shadow_events" if portfolio_id == "shadow" else "verified_events"
    event_repo = DynamicEventRepoAdapter(conn, table)
    event_repo.current_review_id = review_id
    return OrderedEventService(
        conn=conn,
        position_repo=pos_repo,
        event_repo=event_repo,
        processed_repo=NoOpProcessedRepo(),
        review_repo=NoOpReviewRepo(),
        snapshot_repo=NoOpSnapshotRepo(),
        portfolio_id=portfolio_id,
        event_table_name=table,
        parent_source_message_id=parent_source_message_id,
    )


def _has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return any(row["name"] == column_name for row in conn.execute(f"PRAGMA table_info({table_name})"))


def _insert_shadow_event(conn, parent_msg_id, child_msg_id, child_index, clause_text,
                         parsed_json, pos_before, pos_after, status, now):
    if _has_column(conn, "shadow_events", "child_source_message_id"):
        return conn.execute(
            """
            INSERT INTO shadow_events (
                source_message_id, child_source_message_id, parent_source_message_id,
                child_event_index, clause_text, parsed_event_json, position_before_json,
                position_after_json, status, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parent_msg_id, child_msg_id, parent_msg_id, child_index, clause_text,
                parsed_json, pos_before, pos_after, status, now,
            ),
        )
    return conn.execute(
        "INSERT INTO shadow_events (source_message_id, parsed_event_json, position_before_json, position_after_json, status, applied_at) VALUES (?, ?, ?, ?, ?, ?)",
        (parent_msg_id, parsed_json, pos_before, pos_after, status, now)
    )


def _insert_verified_event(conn, parent_msg_id, child_msg_id, child_index, clause_text,
                           approved_json, pos_before, pos_after, review_id, now):
    if _has_column(conn, "verified_events", "child_source_message_id"):
        return conn.execute(
            """
            INSERT INTO verified_events (
                source_message_id, child_source_message_id, parent_source_message_id,
                child_event_index, clause_text, approved_event_json, position_before_json,
                position_after_json, review_id, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parent_msg_id, child_msg_id, parent_msg_id, child_index, clause_text,
                approved_json, pos_before, pos_after, review_id, now,
            ),
        )
    return conn.execute(
        "INSERT INTO verified_events (source_message_id, approved_event_json, position_before_json, position_after_json, review_id, applied_at) VALUES (?, ?, ?, ?, ?, ?)",
        (parent_msg_id, approved_json, pos_before, pos_after, review_id, now)
    )

def _copy_verified_baseline_to_shadow(conn: sqlite3.Connection, event: ParsedTradeEvent) -> None:
    if event.final_action not in {"REDUCE_POSITION", "CLOSE_POSITION"} or not event.symbol:
        return

    repo = SQLitePositionRepository(conn)
    shadow_result = repo.resolve_position(
        portfolio_id="shadow",
        symbol=event.symbol,
        direction=event.direction,
        market_group=event.market_group,
        contract_month=event.contract_month,
        option_type=event.option_type,
        strike_price=event.strike_price,
    )
    if shadow_result.status in {
        IdentityMatchStatus.EXACT_MATCH,
        IdentityMatchStatus.UNIQUE_FALLBACK_MATCH,
        IdentityMatchStatus.AMBIGUOUS_MATCH,
    }:
        return

    verified_result = repo.resolve_position(
        portfolio_id="verified",
        symbol=event.symbol,
        direction=event.direction,
        market_group=event.market_group,
        contract_month=event.contract_month,
        option_type=event.option_type,
        strike_price=event.strike_price,
    )
    verified = verified_result.matched_position
    if not verified:
        return

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO positions (
            portfolio_id, market_group, symbol, contract_month, option_type, strike_price,
            direction, current_allocation_pct, average_entry_price, stop_loss, targets_json,
            status, opened_at, updated_at, version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "shadow", verified.market_group, verified.symbol, verified.contract_month,
            verified.option_type, str(verified.strike_price) if verified.strike_price else "",
            verified.direction, str(verified.current_allocation_pct),
            str(verified.average_entry_price) if verified.average_entry_price else None,
            str(verified.stop_loss) if verified.stop_loss else None,
            json.dumps([str(t) for t in verified.targets]), "OPEN", now, now, verified.version,
        )
    )

def ingest_message(source_message_id: str, raw_text: str, segment_name: str, db_path: Path = DATABASE_PATH) -> dict:
    with transaction(db_path) as conn:
        cur = conn.execute("SELECT 1 FROM incoming_messages WHERE source_message_id = ?", (source_message_id,))
        if cur.fetchone():
            return {"status": "DUPLICATE"}

        now = datetime.now(timezone.utc).isoformat()
        
        parser = HybridParser()
        pre = parser.parse(raw_text)
        existing_long = False
        existing_short = False
        if pre.symbol:
            repo = SQLitePositionRepository(conn)
            long_result = repo.resolve_position(
                portfolio_id="verified",
                symbol=pre.symbol,
                direction="LONG",
                market_group=segment_name,
                contract_month=pre.contract_month,
                option_type=pre.option_type,
                strike_price=pre.strike_price,
            )
            short_result = repo.resolve_position(
                portfolio_id="verified",
                symbol=pre.symbol,
                direction="SHORT",
                market_group=segment_name,
                contract_month=pre.contract_month,
                option_type=pre.option_type,
                strike_price=pre.strike_price,
            )
            existing_long = long_result.status in {
                IdentityMatchStatus.EXACT_MATCH,
                IdentityMatchStatus.UNIQUE_FALLBACK_MATCH,
                IdentityMatchStatus.AMBIGUOUS_MATCH,
            }
            existing_short = short_result.status in {
                IdentityMatchStatus.EXACT_MATCH,
                IdentityMatchStatus.UNIQUE_FALLBACK_MATCH,
                IdentityMatchStatus.AMBIGUOUS_MATCH,
            }

        bundle = parser.parse_bundle(
            raw_text=raw_text,
            source_message_id=source_message_id,
            existing_long=existing_long,
            existing_short=existing_short,
            has_holdings_context=True,
            market_group=segment_name
        )
        for child in bundle.child_events:
            validate(child, confidence=child.ml_confidence, already_processed=False)
        event = bundle.child_events[0]
        if not bundle.is_ordered:
            bundle.auto_apply_eligible = event.auto_apply_eligible
            bundle.needs_review = event.needs_review
        prediction_json = bundle.to_json() if bundle.is_ordered else event.to_json()
        prediction_action = bundle.bundle_type if bundle.is_ordered else event.final_action
        validation_status = "VALID" if (bundle.auto_apply_eligible and not bundle.needs_review) else "INVALID"
        
        conn.execute(
            "INSERT INTO incoming_messages (source_message_id, raw_text, segment_name, created_at, received_at, processing_status, normalized_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_message_id, raw_text, segment_name, now, now, "PENDING_REVIEW", bundle.normalized_text or event.normalized_text)
        )
        
        conn.execute(
            """INSERT INTO parser_predictions (source_message_id, parser_version, model_version, prediction_json, ml_action, ml_confidence, rule_action, final_action, symbol, quantity_percent, validation_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_message_id, "v1", "v1", prediction_json, event.ml_action,
                event.ml_confidence, event.rule_action, prediction_action, event.symbol,
                str(event.quantity_percent) if event.quantity_percent else None,
                validation_status, now
            )
        )

        if bundle.is_ordered:
            if bundle.auto_apply_eligible and not bundle.needs_review:
                for child in bundle.child_events:
                    _copy_verified_baseline_to_shadow(conn, child)
                service = _get_ordered_service(
                    conn, "shadow", parent_source_message_id=source_message_id
                )
                res = service.apply_bundle(bundle)
                if res.status == "OK":
                    conn.execute(
                        "UPDATE incoming_messages SET processing_status = 'SHADOW_APPLIED' WHERE source_message_id = ?",
                        (source_message_id,),
                    )
                    return {"status": "SHADOW_APPLIED", "event": event, "bundle": bundle}
                return {"status": "PENDING_REVIEW", "event": event, "bundle": bundle, "message": res.message}
            return {"status": "PENDING_REVIEW", "event": event, "bundle": bundle}

        safe_for_shadow = (
            event.symbol and
            event.final_action and
            not event.requires_context and
            not event.is_conditional and
            event.final_action != "AMBIGUOUS"
        )

        if safe_for_shadow:
            _copy_verified_baseline_to_shadow(conn, event)
            engine = _get_engine(conn, "shadow", event.to_json())
            res = engine.apply_event(event)
            if res.status == "ERROR":
                print(f"SHADOW ENGINE RETURNED ERROR: {res.message}")

        return {"status": "PENDING_REVIEW", "event": event, "bundle": bundle}

def _lock_message(conn: sqlite3.Connection, source_message_id: str) -> bool:
    cur = conn.execute(
        """
        UPDATE incoming_messages SET processing_status = 'APPROVING'
        WHERE source_message_id = ?
          AND processing_status IN ('PENDING_REVIEW', 'SHADOW_APPLIED')
        """,
        (source_message_id,)
    )
    return cur.rowcount == 1

def _to_prediction_json(obj) -> str:
    return obj.to_json() if hasattr(obj, "to_json") else str(obj)


def _load_prediction_json(conn: sqlite3.Connection, source_message_id: str) -> str:
    cur = conn.execute(
        "SELECT prediction_json FROM parser_predictions WHERE source_message_id = ? ORDER BY id DESC LIMIT 1",
        (source_message_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError("No parser prediction found.")
    return row["prediction_json"]


def _load_prediction_object(conn: sqlite3.Connection, source_message_id: str):
    prediction_json = _load_prediction_json(conn, source_message_id)
    try:
        data = json.loads(prediction_json)
    except Exception:
        return ParsedTradeEvent.from_json(prediction_json)
    if "child_events" in data:
        return ParsedMessageBundle.from_json(prediction_json)
    return ParsedTradeEvent.from_json(prediction_json)


def _record_review(conn: sqlite3.Connection, source_message_id: str, decision: str, reviewer: str, notes: str, orig_event, corrected_event=None, changed_fields: Optional[Dict] = None) -> int:
    cur = conn.execute("SELECT id FROM parser_predictions WHERE source_message_id = ? ORDER BY id DESC LIMIT 1", (source_message_id,))
    row = cur.fetchone()
    prediction_id = row["id"] if row else 0
    now = datetime.now(timezone.utc).isoformat()
    
    cur = conn.execute("""
        INSERT INTO human_reviews (source_message_id, prediction_id, reviewer, decision, original_prediction_json, corrected_event_json, changed_fields_json, review_notes, reviewed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        source_message_id, prediction_id, reviewer, decision, _to_prediction_json(orig_event),
        _to_prediction_json(corrected_event) if corrected_event else None,
        json.dumps(changed_fields) if changed_fields else None,
        notes, now
    ))
    return cur.lastrowid

def _record_label(conn: sqlite3.Connection, msg_id: str, verified_event, was_corrected: bool):
    cur = conn.execute("SELECT raw_text FROM incoming_messages WHERE source_message_id = ?", (msg_id,))
    raw_text = cur.fetchone()["raw_text"]
    now = datetime.now(timezone.utc).isoformat()
    if isinstance(verified_event, ParsedMessageBundle):
        first = verified_event.child_events[0] if verified_event.child_events else ParsedTradeEvent()
        final_action = verified_event.bundle_type
        record_type = "TRADE_ACTION"
        symbol = first.symbol
        direction = first.direction
        quantity_percent = None
        quantity_basis = "ORDERED_CHILDREN"
        prices = []
        stop_loss = None
        targets = []
        ml_action = first.ml_action
        ml_confidence = first.ml_confidence
        rule_action = first.rule_action
    else:
        first = verified_event
        final_action = verified_event.final_action
        record_type = verified_event.record_type
        symbol = verified_event.symbol
        direction = verified_event.direction
        quantity_percent = str(verified_event.quantity_percent) if verified_event.quantity_percent else None
        quantity_basis = verified_event.quantity_basis
        prices = verified_event.execution_prices
        stop_loss = verified_event.stop_loss
        targets = verified_event.targets
        ml_action = verified_event.ml_action
        ml_confidence = verified_event.ml_confidence
        rule_action = verified_event.rule_action
    conn.execute("""
        INSERT INTO verified_labels (
            source_message_id, raw_text, verified_record_type, verified_action, verified_symbol, verified_direction,
            verified_quantity_percent, verified_quantity_basis, verified_entry_prices_json, verified_stop_loss, verified_targets_json,
            original_ml_action, original_ml_confidence, original_rule_action, was_corrected, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        msg_id, raw_text, record_type, final_action, symbol, direction,
        quantity_percent, quantity_basis,
        json.dumps([str(p) for p in prices]),
        str(stop_loss) if stop_loss else None,
        json.dumps([str(t) for t in targets]),
        ml_action, ml_confidence, rule_action, int(was_corrected), now
    ))

def approve_as_parsed(source_message_id: str, reviewer: str, db_path: Path = DATABASE_PATH) -> dict:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _lock_message(conn, source_message_id): return {"status": "ALREADY_REVIEWED"}

        prediction = _load_prediction_object(conn, source_message_id)
        review_id = _record_review(conn, source_message_id, "APPROVED", reviewer, "", prediction)

        try:
            if isinstance(prediction, ParsedMessageBundle) and prediction.is_ordered:
                service = _get_ordered_service(
                    conn, "verified", review_id=review_id,
                    parent_source_message_id=source_message_id,
                )
                res = service.apply_bundle(prediction)
                if res.status != "OK":
                    raise ValueError(f"Engine rejected ordered event: {res.message}")
            else:
                event = prediction
                engine = _get_engine(conn, "verified", event.to_json(), review_id)
                res = engine.apply_event(event)
                if res.status != "OK":
                    raise ValueError(f"Engine rejected event: {res.message}")
        except Exception as e:
            conn.rollback()
            return {"status": "ERROR", "message": str(e)}
            
        conn.execute("UPDATE incoming_messages SET processing_status = 'APPROVED' WHERE source_message_id = ?", (source_message_id,))
        _record_label(conn, source_message_id, prediction, was_corrected=False)
        return {"status": "APPROVED"}

def edit_and_approve(source_message_id: str, reviewer: str, corrected_event, changed_fields: Dict, notes: str = "", db_path: Path = DATABASE_PATH) -> dict:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _lock_message(conn, source_message_id): return {"status": "ALREADY_REVIEWED"}

        orig_event = _load_prediction_object(conn, source_message_id)
        review_id = _record_review(conn, source_message_id, "APPROVED_WITH_CORRECTION", reviewer, notes, orig_event, corrected_event, changed_fields)

        try:
            if isinstance(corrected_event, ParsedMessageBundle) and corrected_event.is_ordered:
                service = _get_ordered_service(
                    conn, "verified", review_id=review_id,
                    parent_source_message_id=source_message_id,
                )
                res = service.apply_bundle(corrected_event)
                if res.status != "OK":
                    raise ValueError(f"Engine rejected corrected ordered event: {res.message}")
            else:
                engine = _get_engine(conn, "verified", corrected_event.to_json(), review_id)
                res = engine.apply_event(corrected_event)
                if res.status != "OK":
                    raise ValueError(f"Engine rejected corrected event: {res.message}")
        except Exception as e:
            conn.rollback()
            return {"status": "ERROR", "message": str(e)}
            
        conn.execute("UPDATE incoming_messages SET processing_status = 'APPROVED_WITH_CORRECTION' WHERE source_message_id = ?", (source_message_id,))
        _record_label(conn, source_message_id, corrected_event, was_corrected=True)
        return {"status": "APPROVED"}

def reject(source_message_id: str, reviewer: str, notes: str = "", db_path: Path = DATABASE_PATH) -> dict:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _lock_message(conn, source_message_id): return {"status": "ALREADY_REVIEWED"}
        
        orig_event = _load_prediction_object(conn, source_message_id)
        
        _record_review(conn, source_message_id, "REJECTED", reviewer, notes, orig_event)
        conn.execute("UPDATE incoming_messages SET processing_status = 'REJECTED' WHERE source_message_id = ?", (source_message_id,))
        label_event = ParsedTradeEvent.from_json(_to_prediction_json(orig_event)) if not isinstance(orig_event, ParsedMessageBundle) else ParsedTradeEvent(
            source_message_id=source_message_id,
            raw_text=orig_event.raw_text,
            final_action="REJECTED",
            record_type="HUMAN_REVIEW_DECISION",
        )
        label_event.final_action = "REJECTED"
        label_event.record_type = "HUMAN_REVIEW_DECISION"
        _record_label(conn, source_message_id, label_event, was_corrected=True)
        return {"status": "REJECTED"}

def mark_non_trade(source_message_id: str, reviewer: str, notes: str = "", db_path: Path = DATABASE_PATH) -> dict:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _lock_message(conn, source_message_id): return {"status": "ALREADY_REVIEWED"}
        
        orig_event = _load_prediction_object(conn, source_message_id)
        
        _record_review(conn, source_message_id, "NON_TRADE", reviewer, notes, orig_event)
        conn.execute("UPDATE incoming_messages SET processing_status = 'NON_TRADE' WHERE source_message_id = ?", (source_message_id,))
        
        # Mark as non-trade in verified_labels
        label_event = ParsedTradeEvent(
            source_message_id=source_message_id,
            raw_text=orig_event.raw_text,
            final_action="NON_TRADE",
            record_type="HUMAN_REVIEW_DECISION",
        )
        _record_label(conn, source_message_id, label_event, was_corrected=True)
        
        return {"status": "NON_TRADE"}

def mark_needs_context(source_message_id: str, reviewer: str, notes: str = "", db_path: Path = DATABASE_PATH) -> dict:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _lock_message(conn, source_message_id): return {"status": "ALREADY_REVIEWED"}
        
        orig_event = _load_prediction_object(conn, source_message_id)
        
        _record_review(conn, source_message_id, "NEEDS_CONTEXT", reviewer, notes, orig_event)
        conn.execute("UPDATE incoming_messages SET processing_status = 'NEEDS_CONTEXT' WHERE source_message_id = ?", (source_message_id,))
        label_event = ParsedTradeEvent.from_json(_to_prediction_json(orig_event)) if not isinstance(orig_event, ParsedMessageBundle) else ParsedTradeEvent(
            source_message_id=source_message_id,
            raw_text=orig_event.raw_text,
            final_action="NEEDS_CONTEXT",
            record_type="HUMAN_REVIEW_DECISION",
        )
        label_event.final_action = "NEEDS_CONTEXT"
        label_event.record_type = "HUMAN_REVIEW_DECISION"
        _record_label(conn, source_message_id, label_event, was_corrected=True)
        return {"status": "NEEDS_CONTEXT"}

def mark_duplicate(source_message_id: str, reviewer: str, notes: str = "", db_path: Path = DATABASE_PATH) -> dict:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _lock_message(conn, source_message_id): return {"status": "ALREADY_REVIEWED"}
        
        orig_event = _load_prediction_object(conn, source_message_id)
        
        _record_review(conn, source_message_id, "IGNORED_DUPLICATE", reviewer, notes, orig_event)
        conn.execute("UPDATE incoming_messages SET processing_status = 'IGNORED' WHERE source_message_id = ?", (source_message_id,))
        return {"status": "IGNORED"}

from contextlib import closing

def get_pending_reviews(db_path: Path = DATABASE_PATH) -> List[Dict]:
    with closing(get_connection(db_path)) as conn:
        cur = conn.execute("""
            SELECT i.source_message_id, i.raw_text, i.normalized_text, i.segment_name, i.received_at, p.prediction_json
            FROM incoming_messages i
            JOIN parser_predictions p ON i.source_message_id = p.source_message_id
            WHERE i.processing_status IN ('PENDING_REVIEW', 'SHADOW_APPLIED')
            ORDER BY i.id ASC
        """)
        return [dict(row) for row in cur.fetchall()]

def get_review_details(source_message_id: str, db_path: Path = DATABASE_PATH) -> Dict:
    with closing(get_connection(db_path)) as conn:
        cur = conn.execute("""
            SELECT i.*, p.prediction_json, p.validation_status
            FROM incoming_messages i
            JOIN parser_predictions p ON i.source_message_id = p.source_message_id
            WHERE i.source_message_id = ?
            ORDER BY p.id DESC LIMIT 1
        """, (source_message_id,))
        row = cur.fetchone()
        if not row:
            return None
            
        return dict(row)
