import sqlite3
from decimal import Decimal

from src.database import _SCHEMA_SQL, init_db, get_connection
from src.holdings_engine import HoldingsEngine
from src.position_identity import (
    IdentityMatchStatus,
    audit_duplicate_identity_keys,
    canonical_market_group,
    canonical_symbol,
)
from src.schemas import ParsedTradeEvent, PositionState
from src.sqlite_repository import (
    SQLitePositionRepository,
    SQLiteTradeEventRepository,
    SQLiteProcessedMessageRepository,
    SQLiteReviewQueueRepository,
    SQLiteSnapshotRepository,
)
from scripts.migrate_shadow_tables import apply_migration as apply_shadow_migration
from scripts.migrate_position_identity import (
    apply_migration as apply_identity_migration,
    audit_position_identity_duplicates,
)


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    return conn


def save(repo, **kwargs):
    defaults = {
        "portfolio_id": "verified",
        "market_group": "GLOBAL_EQUITY",
        "symbol": "AAPL",
        "contract_month": "",
        "option_type": "",
        "strike_price": None,
        "direction": "LONG",
        "current_allocation_pct": Decimal("100"),
        "status": "OPEN",
    }
    defaults.update(kwargs)
    return repo.save_position(PositionState(**defaults))


def make_event(**kwargs):
    defaults = {
        "source_message_id": "evt-1",
        "raw_text": "TEST",
        "normalized_text": "TEST",
        "record_type": "TRADE_ACTION",
        "final_action": "REDUCE_POSITION",
        "symbol": "AAPL",
        "quantity_percent": Decimal("50"),
        "quantity_basis": "CURRENT_POSITION",
    }
    defaults.update(kwargs)
    return ParsedTradeEvent(**defaults)


def make_engine(conn):
    return HoldingsEngine(
        position_repo=SQLitePositionRepository(conn),
        event_repo=SQLiteTradeEventRepository(conn),
        processed_repo=SQLiteProcessedMessageRepository(conn),
        review_repo=SQLiteReviewQueueRepository(conn),
        snapshot_repo=SQLiteSnapshotRepository(conn),
        portfolio_id="verified",
    )


def test_symbol_and_market_normalisation_examples():
    assert canonical_symbol("BANK NIFTY") == "BANKNIFTY"
    assert canonical_symbol("SPX") == "SP500"
    assert canonical_symbol("S&P 500") == "SP500"
    assert canonical_symbol("NG") == "NATURAL_GAS"
    assert canonical_symbol("CRUDEOILM") == "CRUDE_MINI"
    assert canonical_market_group("International Market", "AAPL") == "GLOBAL_EQUITY"
    assert canonical_market_group("International Market", "SP500") == "GLOBAL_INDEX_OR_MACRO"
    assert canonical_market_group("International Market", "GOLD") == "GLOBAL_COMMODITY"


def test_simple_unique_aapl_long_lookup_and_market_alias():
    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    save(repo, symbol="AAPL", market_group="GLOBAL_EQUITY", direction="LONG")

    result = repo.resolve_position(
        "verified", "AAPL", market_group="International Market", direction="LONG"
    )

    assert result.status == IdentityMatchStatus.UNIQUE_FALLBACK_MATCH
    assert result.matched_position.symbol == "AAPL"
    assert result.matched_position.market_group == "GLOBAL_EQUITY"


def test_same_symbol_in_two_markets_does_not_collide():
    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    save(repo, symbol="ABC", market_group="GLOBAL_EQUITY", direction="LONG")
    save(repo, symbol="ABC", market_group="INDIA_EQUITY", direction="LONG")

    result = repo.resolve_position(
        "verified", "ABC", market_group="INDIA_EQUITY", direction="LONG"
    )

    assert result.status == IdentityMatchStatus.UNIQUE_FALLBACK_MATCH
    assert result.matched_position.market_group == "INDIA_EQUITY"


def test_contracts_and_sides_remain_separate():
    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    sep = save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO",
               contract_month="2026-09", direction="LONG")
    dec = save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO",
               contract_month="2026-12", direction="LONG")
    short = save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO",
                 contract_month="2026-09", direction="SHORT")

    sep_result = repo.resolve_position(
        "verified", "SP500", market_group="GLOBAL_INDEX_OR_MACRO",
        contract_month="SEP 2026", direction="LONG"
    )
    short_result = repo.resolve_position(
        "verified", "SP500", market_group="GLOBAL_INDEX_OR_MACRO",
        contract_month="2026-09", direction="SHORT"
    )

    assert sep_result.matched_position.position_id == sep.position_id
    assert sep_result.matched_position.position_id != dec.position_id
    assert short_result.matched_position.position_id == short.position_id


def test_contract_specific_reduction_affects_only_that_contract():
    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    sep = save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO",
               contract_month="2026-09", direction="LONG")
    dec = save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO",
               contract_month="2026-12", direction="LONG")
    engine = make_engine(conn)

    result = engine.apply_event(make_event(
        source_message_id="reduce-sep",
        symbol="SP500",
        market_group="GLOBAL_INDEX_OR_MACRO",
        contract_month="2026-09",
    ))

    assert result.status == "OK"
    assert repo.get_position("verified", "SP500", market_group="GLOBAL_INDEX_OR_MACRO",
                             contract_month="2026-09").current_allocation_pct == Decimal("50.0000000000")
    assert repo.get_position("verified", "SP500", market_group="GLOBAL_INDEX_OR_MACRO",
                             contract_month="2026-12").current_allocation_pct == Decimal("100")


def test_stock_option_strike_and_option_type_remain_separate():
    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    stock = save(repo, symbol="MSFT", market_group="GLOBAL_EQUITY", direction="LONG")
    call450 = save(repo, symbol="MSFT", market_group="GLOBAL_EQUITY",
                   contract_month="2026-08", option_type="CALL",
                   strike_price=Decimal("450"), direction="LONG")
    call500 = save(repo, symbol="MSFT", market_group="GLOBAL_EQUITY",
                   contract_month="2026-08", option_type="CALL",
                   strike_price=Decimal("500"), direction="LONG")
    put450 = save(repo, symbol="MSFT", market_group="GLOBAL_EQUITY",
                  contract_month="2026-08", option_type="PUT",
                  strike_price=Decimal("450"), direction="LONG")

    assert repo.resolve_position(
        "verified", "MSFT", market_group="GLOBAL_EQUITY", direction="LONG",
        contract_month="", option_type="", strike_price=""
    ).matched_position.position_id == stock.position_id
    assert repo.resolve_position(
        "verified", "MSFT", market_group="GLOBAL_EQUITY", direction="LONG",
        contract_month="AUGUST 2026", option_type="CE", strike_price="450.0"
    ).matched_position.position_id == call450.position_id
    assert repo.resolve_position(
        "verified", "MSFT", market_group="GLOBAL_EQUITY", direction="LONG",
        contract_month="2026-08", option_type="CALL", strike_price="500"
    ).matched_position.position_id == call500.position_id
    assert repo.resolve_position(
        "verified", "MSFT", market_group="GLOBAL_EQUITY", direction="LONG",
        contract_month="2026-08", option_type="PE", strike_price="450"
    ).matched_position.position_id == put450.position_id


def test_supplied_option_fields_do_not_match_underlying_stock():
    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    save(repo, symbol="MSFT", market_group="GLOBAL_EQUITY", direction="LONG")

    result = repo.resolve_position(
        "verified", "MSFT", market_group="GLOBAL_EQUITY",
        contract_month="2026-08", option_type="CALL", strike_price="450",
        direction="LONG",
    )

    assert result.status == IdentityMatchStatus.NO_MATCH


def test_exact_full_key_and_unique_fallback_statuses():
    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    save(repo, symbol="AAPL", market_group="GLOBAL_EQUITY", direction="LONG")

    exact = repo.resolve_position(
        "verified", "AAPL", market_group="GLOBAL_EQUITY",
        contract_month="", option_type="", strike_price="", direction="LONG"
    )
    fallback = repo.resolve_position("verified", "AAPL")

    assert exact.status == IdentityMatchStatus.EXACT_MATCH
    assert fallback.status == IdentityMatchStatus.UNIQUE_FALLBACK_MATCH


def test_incomplete_multiple_contracts_and_both_sides_are_ambiguous():
    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO",
         contract_month="2026-09", direction="LONG")
    save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO",
         contract_month="2026-12", direction="LONG")

    assert repo.resolve_position("verified", "SP500").status == IdentityMatchStatus.AMBIGUOUS_MATCH

    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO", direction="LONG")
    save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO", direction="SHORT")

    assert repo.resolve_position("verified", "SP500").status == IdentityMatchStatus.AMBIGUOUS_MATCH


def test_supplied_conflicting_contract_returns_no_match():
    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO",
         contract_month="2026-09", direction="LONG")

    result = repo.resolve_position(
        "verified", "SP500", market_group="GLOBAL_INDEX_OR_MACRO",
        contract_month="2026-12", direction="LONG"
    )

    assert result.status == IdentityMatchStatus.NO_MATCH


def test_guarded_fallback_never_chooses_first_arbitrary_row():
    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    first = save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO",
                 contract_month="2026-09", direction="LONG")
    save(repo, symbol="SP500", market_group="GLOBAL_INDEX_OR_MACRO",
         contract_month="2026-12", direction="LONG")

    result = repo.resolve_position("verified", "SP500", direction="LONG")

    assert result.status == IdentityMatchStatus.AMBIGUOUS_MATCH
    assert result.matched_position is None
    assert first.current_allocation_pct == Decimal("100")


def test_no_wrong_position_is_modified_after_ambiguous_lookup():
    conn = make_conn()
    repo = SQLitePositionRepository(conn)
    save(repo, symbol="MSFT", market_group="GLOBAL_EQUITY", direction="LONG")
    option = save(repo, symbol="MSFT", market_group="GLOBAL_EQUITY",
                  contract_month="2026-08", option_type="CALL",
                  strike_price=Decimal("450"), direction="LONG")
    engine = make_engine(conn)

    result = engine.apply_event(make_event(source_message_id="amb-msft", symbol="MSFT"))

    assert result.status == "MANUAL_REVIEW"
    assert "AMBIGUOUS_POSITION_IDENTITY" in result.message
    assert repo.get_position("verified", "MSFT", market_group="GLOBAL_EQUITY",
                             contract_month="", option_type="", strike_price="",
                             direction="LONG").current_allocation_pct == Decimal("100")
    assert repo.get_position("verified", "MSFT", market_group="GLOBAL_EQUITY",
                             contract_month="2026-08", option_type="CALL",
                             strike_price="450", direction="LONG").position_id == option.position_id


def test_duplicate_audit_reports_normalised_duplicates():
    positions = [
        PositionState(position_id=1, portfolio_id="verified", market_group=None,
                      symbol="NG", direction="LONG"),
        PositionState(position_id=2, portfolio_id="verified", market_group="UNKNOWN",
                      symbol="NATURAL_GAS", direction="LONG"),
    ]

    report = audit_duplicate_identity_keys(positions)

    assert report
    assert report[0]["position_ids"] == [1, 2]


def test_migration_runs_twice_safely_and_preserves_historical_tables(tmp_path):
    db_path = tmp_path / "phase2.db"
    init_db(db_path)
    apply_shadow_migration(db_path)

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO positions (
                portfolio_id, market_group, symbol, contract_month,
                option_type, strike_price, direction, current_allocation_pct, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("verified", "International Market", "AAPL", "", "", "", "LONG", "100", "OPEN"),
        )
        conn.commit()

    assert audit_position_identity_duplicates(db_path) == []
    apply_identity_migration(db_path)
    apply_identity_migration(db_path)

    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM positions").fetchone()
        assert row["market_group"] == "GLOBAL_EQUITY"
        assert row["contract_month"] == ""
        assert row["option_type"] == ""
        assert row["strike_price"] == ""
        assert conn.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE migration_name='002_position_identity_indexes'"
        ).fetchone()[0] == 1
