import json
import sqlite3
from decimal import Decimal

from src.database import _SCHEMA_SQL, get_connection, init_db
from src.hybrid_parser import HybridParser
from src.ordered_event_service import OrderedEventService
from src.schemas import ParsedMessageBundle, PositionState
from src.sqlite_repository import (
    SQLitePositionRepository,
    SQLiteProcessedMessageRepository,
    SQLiteReviewQueueRepository,
    SQLiteSnapshotRepository,
    SQLiteTradeEventRepository,
)
from src.shadow_service import (
    approve_as_parsed,
    edit_and_approve,
    ingest_message,
    reject,
)
from scripts.migrate_shadow_tables import apply_migration


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    return conn


def make_parser():
    return HybridParser()


def save_position(conn, **kwargs):
    defaults = {
        "portfolio_id": "default",
        "market_group": "GLOBAL_EQUITY",
        "symbol": "TSLA",
        "direction": "LONG",
        "current_allocation_pct": Decimal("100"),
        "status": "OPEN",
    }
    defaults.update(kwargs)
    return SQLitePositionRepository(conn).save_position(PositionState(**defaults))


def parse_bundle(text, source_id="parent-1", existing_long=True, existing_short=False):
    return make_parser().parse_bundle(
        text,
        source_message_id=source_id,
        existing_long=existing_long,
        existing_short=existing_short,
        has_holdings_context=True,
        market_group="International Market",
    )


def make_service(conn):
    return OrderedEventService(
        conn=conn,
        position_repo=SQLitePositionRepository(conn),
        event_repo=SQLiteTradeEventRepository(conn),
        processed_repo=SQLiteProcessedMessageRepository(conn),
        review_repo=SQLiteReviewQueueRepository(conn),
        snapshot_repo=SQLiteSnapshotRepository(conn),
        portfolio_id="default",
        event_table_name="trade_events",
        mark_parent_processed=True,
    )


def test_long_to_short_ampersand_produces_two_children():
    bundle = parse_bundle(
        "FULL PROFIT BOOK IN TSLA @1124 & 50% SELL TSLA @1124 SL 1200 TGT 1100-1050"
    )

    assert bundle.is_ordered
    assert bundle.bundle_type == "ORDERED_REVERSAL"
    assert len(bundle.child_events) == 2
    assert [c.source_message_id for c in bundle.child_events] == ["parent-1#1", "parent-1#2"]
    assert bundle.child_events[0].raw_text == bundle.raw_text
    assert bundle.child_events[0].clause_text == "FULL PROFIT BOOK IN TSLA @1124"
    assert bundle.child_events[1].clause_text.startswith("50% SELL TSLA")


def test_short_to_long_ampersand_produces_two_children():
    bundle = parse_bundle(
        "FULL PROFIT BOOK IN TSLA @1124 & 50% BUY TSLA @1124 SL 1050 TGT 1200-1250",
        existing_long=False,
        existing_short=True,
    )

    assert bundle.is_ordered
    assert not bundle.needs_review
    assert bundle.child_events[0].direction == "SHORT"
    assert bundle.child_events[1].direction == "LONG"


def test_newline_and_semicolon_separators_work():
    newline_bundle = parse_bundle("EXIT FROM TSLA\nSELL 50% TSLA @1124")
    semicolon_bundle = parse_bundle("SL TOUCHED IN TSLA; SELL 50% TSLA @1124")

    assert newline_bundle.is_ordered and not newline_bundle.needs_review
    assert semicolon_bundle.is_ordered and not semicolon_bundle.needs_review


def test_target_range_and_and_prose_are_not_split():
    range_bundle = parse_bundle("SELL 50% TSLA @1124 SL 1200 TGT 1100-1050")
    and_bundle = parse_bundle("FULL PROFIT BOOK IN TSLA AND WAIT FOR NEXT TRADE")

    assert not range_bundle.is_ordered
    assert not and_bundle.is_ordered


def test_too_many_actionable_clauses_goes_to_review():
    bundle = parse_bundle("EXIT FROM TSLA & SELL 50% TSLA & BUY 50% TSLA")

    assert bundle.needs_review
    assert "ORDERED_TOO_MANY_ACTIONABLE_CLAUSES" in bundle.validation_errors


def test_missing_symbol_different_symbol_contract_and_part_profit_review():
    missing = parse_bundle("FULL PROFIT BOOK @1124 & SELL 50% TSLA")
    different = parse_bundle("FULL PROFIT BOOK IN TSLA & SELL 50% NVDA")
    contract = parse_bundle("FULL PROFIT BOOK IN TSLA JUNE & SELL 50% TSLA JULY")
    part = parse_bundle("50% PROFIT BOOK IN TSLA & SELL 50% TSLA")

    assert missing.needs_review
    assert "ORDERED_SPLIT_AMBIGUOUS" in missing.validation_errors
    assert "ORDERED_CHILD_IDENTITY_MISMATCH" in different.validation_errors
    assert "ORDERED_CHILD_IDENTITY_MISMATCH" in contract.validation_errors
    assert "ORDERED_CHILD_1_NOT_FULL_CLOSE" in part.validation_errors


def test_conditional_correction_and_single_clause_behaviour():
    conditional = parse_bundle("IF TRIGGERED FULL PROFIT BOOK IN TSLA & SELL 50% TSLA")
    correction = parse_bundle("CORRECTION FULL PROFIT BOOK IN TSLA & SELL 50% TSLA")
    single = parse_bundle("BUY 50% TSLA", existing_long=False)

    assert conditional.needs_review
    assert correction.needs_review
    assert not single.is_ordered
    assert len(single.child_events) == 1
    assert single.child_events[0].final_action == "OPEN_LONG"


def test_same_side_second_clause_is_not_reversal():
    bundle = parse_bundle("FULL PROFIT BOOK IN TSLA & BUY 50% TSLA")

    assert bundle.needs_review
    assert "ORDERED_NOT_OPPOSITE_DIRECTION" in bundle.validation_errors


def test_long_to_short_execution_closes_long_and_opens_short():
    conn = make_conn()
    save_position(conn, portfolio_id="default", market_group="GLOBAL_EQUITY")
    bundle = parse_bundle(
        "FULL PROFIT BOOK IN TSLA @1124 & 50% SELL TSLA @1124 SL 1200 TGT 1100-1050"
    )

    result = make_service(conn).apply_bundle(bundle)

    assert result.status == "OK"
    repo = SQLitePositionRepository(conn)
    closed_long = repo.list_all_positions("default")[0]
    short = repo.get_position("default", "TSLA", direction="SHORT", market_group="GLOBAL_EQUITY")
    assert closed_long.status == "CLOSED"
    assert closed_long.current_allocation_pct == Decimal("0")
    assert short.current_allocation_pct == Decimal("50")
    assert short.stop_loss == Decimal("1200")
    assert short.targets == [Decimal("1100"), Decimal("1050")]


def test_short_to_long_execution():
    conn = make_conn()
    save_position(conn, portfolio_id="default", market_group="GLOBAL_EQUITY", direction="SHORT")
    bundle = parse_bundle(
        "FULL PROFIT BOOK IN TSLA @1124 & 50% BUY TSLA @1124 SL 1050 TGT 1200-1250",
        existing_long=False,
        existing_short=True,
    )

    result = make_service(conn).apply_bundle(bundle)

    assert result.status == "OK"
    long_pos = SQLitePositionRepository(conn).get_position(
        "default", "TSLA", direction="LONG", market_group="GLOBAL_EQUITY"
    )
    assert long_pos.current_allocation_pct == Decimal("50")
    assert long_pos.stop_loss == Decimal("1050")


def test_child_1_failure_and_missing_prior_position_change_nothing():
    conn = make_conn()
    bundle = parse_bundle(
        "FULL PROFIT BOOK IN TSLA @1124 & 50% SELL TSLA @1124",
        existing_long=True,
    )

    result = make_service(conn).apply_bundle(bundle)

    assert result.status == "MANUAL_REVIEW"
    assert result.review_reason == "ORDERED_CHILD_1_NO_POSITION"
    assert SQLitePositionRepository(conn).list_all_positions("default") == []
    assert conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0] == 0


class FailingSecondEventRepo(SQLiteTradeEventRepository):
    def __init__(self, conn):
        super().__init__(conn)
        self.calls = 0

    def insert_event(self, event_record: dict) -> int:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("forced child 2 event failure")
        return super().insert_event(event_record)


def test_child_2_failure_rolls_back_child_1_at_sqlite_level():
    conn = make_conn()
    save_position(conn, portfolio_id="default", market_group="GLOBAL_EQUITY")
    bundle = parse_bundle("FULL PROFIT BOOK IN TSLA @1124 & 50% SELL TSLA @1124")
    service = OrderedEventService(
        conn=conn,
        position_repo=SQLitePositionRepository(conn),
        event_repo=FailingSecondEventRepo(conn),
        processed_repo=SQLiteProcessedMessageRepository(conn),
        review_repo=SQLiteReviewQueueRepository(conn),
        snapshot_repo=SQLiteSnapshotRepository(conn),
        portfolio_id="default",
        event_table_name="trade_events",
    )

    result = service.apply_bundle(bundle)

    assert result.status == "MANUAL_REVIEW"
    long_pos = SQLitePositionRepository(conn).get_position(
        "default", "TSLA", direction="LONG", market_group="GLOBAL_EQUITY"
    )
    assert long_pos.current_allocation_pct == Decimal("100")
    assert conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0] == 0


def test_ambiguous_identity_existing_opposite_and_same_side_block():
    conn = make_conn()
    save_position(conn, symbol="TSLA", market_group="GLOBAL_EQUITY", contract_month="2026-06")
    save_position(conn, symbol="TSLA", market_group="GLOBAL_EQUITY", contract_month="2026-07")
    ambiguous = parse_bundle("FULL PROFIT BOOK IN TSLA & SELL 50% TSLA")
    assert make_service(conn).apply_bundle(ambiguous).review_reason == "ORDERED_CHILD_1_AMBIGUOUS_IDENTITY"

    conn = make_conn()
    save_position(conn, market_group="GLOBAL_EQUITY", direction="LONG")
    save_position(conn, market_group="GLOBAL_EQUITY", direction="SHORT")
    existing_opp = parse_bundle("FULL PROFIT BOOK IN TSLA & SELL 50% TSLA")
    assert make_service(conn).apply_bundle(existing_opp).review_reason == "ORDERED_EXISTING_OPPOSITE_POSITION"


def test_reprocessing_successful_parent_creates_no_duplicates_and_partial_detected():
    conn = make_conn()
    save_position(conn, portfolio_id="default", market_group="GLOBAL_EQUITY")
    bundle = parse_bundle("FULL PROFIT BOOK IN TSLA & SELL 50% TSLA")
    service = make_service(conn)

    assert service.apply_bundle(bundle).status == "OK"
    assert service.apply_bundle(bundle).status == "ALREADY_PROCESSED"
    assert conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM position_snapshots").fetchone()[0] == 2

    conn = make_conn()
    save_position(conn, portfolio_id="default", market_group="GLOBAL_EQUITY")
    bundle = parse_bundle("FULL PROFIT BOOK IN TSLA & SELL 50% TSLA")
    conn.execute(
        "INSERT INTO processed_messages (source_message_id, processing_status) VALUES (?, ?)",
        (bundle.child_events[0].source_message_id, "APPLIED"),
    )
    assert make_service(conn).apply_bundle(bundle).review_reason == "PARTIAL_ORDERED_SEQUENCE_DETECTED"


def test_shadow_and_verified_ordered_workflow(tmp_path):
    db_path = tmp_path / "ordered_shadow.db"
    init_db(db_path)
    apply_migration(db_path)
    with get_connection(db_path) as conn:
        SQLitePositionRepository(conn).save_position(PositionState(
            portfolio_id="verified",
            market_group="GLOBAL_EQUITY",
            symbol="TSLA",
            direction="LONG",
            current_allocation_pct=Decimal("100"),
            status="OPEN",
        ))
        conn.commit()

    res = ingest_message(
        "ord-1",
        "FULL PROFIT BOOK IN TSLA @1124 & 50% SELL TSLA @1124 SL 1200 TGT 1100-1050",
        "International Market",
        db_path,
    )
    assert res["status"] == "SHADOW_APPLIED"

    with get_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM shadow_events WHERE source_message_id='ord-1'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM verified_events WHERE source_message_id='ord-1'").fetchone()[0] == 0
        assert conn.execute("SELECT current_allocation_pct FROM positions WHERE portfolio_id='verified' AND symbol='TSLA' AND direction='LONG'").fetchone()[0] == "100"

    assert approve_as_parsed("ord-1", "Reviewer", db_path)["status"] == "APPROVED"
    assert approve_as_parsed("ord-1", "Reviewer", db_path)["status"] == "ALREADY_REVIEWED"

    with get_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM verified_events WHERE source_message_id='ord-1'").fetchone()[0] == 2
        verified_short = conn.execute(
            "SELECT * FROM positions WHERE portfolio_id='verified' AND symbol='TSLA' AND direction='SHORT' AND status='OPEN'"
        ).fetchone()
        assert verified_short["current_allocation_pct"] == "50"
        prediction = conn.execute("SELECT prediction_json FROM parser_predictions WHERE source_message_id='ord-1'").fetchone()[0]
        assert ParsedMessageBundle.from_json(prediction).child_events[1].clause_text.startswith("50% SELL")


def test_rejection_preserves_shadow_history_and_edit_approve_corrects_children(tmp_path):
    db_path = tmp_path / "ordered_shadow_edit.db"
    init_db(db_path)
    apply_migration(db_path)
    with get_connection(db_path) as conn:
        for symbol in ("TSLA", "NVDA"):
            SQLitePositionRepository(conn).save_position(PositionState(
                portfolio_id="verified",
                market_group="GLOBAL_EQUITY",
                symbol=symbol,
                direction="LONG",
                current_allocation_pct=Decimal("100"),
                status="OPEN",
            ))
        conn.commit()

    ingest_message("ord-reject", "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA", "International Market", db_path)
    assert reject("ord-reject", "Reviewer", "No", db_path)["status"] == "REJECTED"
    with get_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM shadow_events WHERE source_message_id='ord-reject'").fetchone()[0] == 2
        assert conn.execute("SELECT current_allocation_pct FROM positions WHERE portfolio_id='verified' AND symbol='TSLA' AND direction='LONG'").fetchone()[0] == "100"

    res = ingest_message("ord-edit", "FULL PROFIT BOOK IN NVDA & SELL 50% NVDA", "International Market", db_path)
    corrected = res["bundle"]
    corrected.child_events[1].quantity_percent = Decimal("75")
    corrected.child_events[1].entry_capacity_pct = Decimal("75")
    assert edit_and_approve(
        "ord-edit",
        "Reviewer",
        corrected,
        {"2": {"quantity_percent": "75"}},
        "increase short size",
        db_path,
    )["status"] == "APPROVED"
    with get_connection(db_path) as conn:
        approved_json = conn.execute(
            "SELECT corrected_event_json FROM human_reviews WHERE source_message_id='ord-edit'"
        ).fetchone()[0]
        assert json.loads(approved_json)["child_events"][1]["quantity_percent"] == "75"
        short = conn.execute(
            "SELECT current_allocation_pct FROM positions WHERE portfolio_id='verified' AND symbol='NVDA' AND direction='SHORT'"
        ).fetchone()[0]
        assert short == "75"
