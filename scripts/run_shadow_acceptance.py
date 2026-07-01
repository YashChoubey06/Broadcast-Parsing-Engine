from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import sklearn

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from scripts.migrate_shadow_tables import apply_migration
from src.config import PROJECT_ROOT, REPORTS_DIR, STORAGE_DIR
from src.database import get_connection, init_db
from src.export_verified_training_data import export_labels
from src.import_verified_positions import import_positions
from src.schemas import ParsedTradeEvent
from src.shadow_metrics import generate_shadow_metrics
from src.shadow_service import (
    approve_as_parsed,
    edit_and_approve,
    get_pending_reviews,
    get_review_details,
    ingest_message,
    mark_needs_context,
    mark_non_trade,
    reject,
)


ACCEPTANCE_DB = (STORAGE_DIR / "shadow_acceptance.db").resolve()
INTERPRETER = (PROJECT_ROOT / ".venv" / "Scripts" / "python.exe").resolve()
REPORT_PREFIX = REPORTS_DIR / "shadow_acceptance"
REQUIRED_BASE_TABLES = [
    "positions",
    "trade_events",
    "processed_messages",
    "manual_review_queue",
    "position_snapshots",
]
REQUIRED_SHADOW_TABLES = [
    "incoming_messages",
    "parser_predictions",
    "human_reviews",
    "shadow_events",
    "verified_events",
    "verified_labels",
]


@dataclass
class AcceptanceState:
    checks: list[dict[str, Any]] = field(default_factory=list)
    scenario_results: dict[str, Any] = field(default_factory=dict)
    defects_fixed: list[dict[str, str]] = field(default_factory=list)
    report_artifacts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    full_suite: dict[str, Any] = field(default_factory=dict)
    streamlit: dict[str, Any] = field(default_factory=dict)


def fail(state: AcceptanceState, scenario: str, message: str) -> None:
    state.failures.append(f"{scenario}: {message}")
    state.checks.append({"scenario": scenario, "status": "FAIL", "detail": message})
    raise AssertionError(f"{scenario}: {message}")


def check(state: AcceptanceState, scenario: str, condition: bool, detail: str) -> None:
    if not condition:
        fail(state, scenario, detail)
    state.checks.append({"scenario": scenario, "status": "PASS", "detail": detail})


def assert_acceptance_db(db_path: Path) -> None:
    resolved = db_path.resolve()
    if resolved != ACCEPTANCE_DB:
        raise RuntimeError(f"Acceptance harness may only use {ACCEPTANCE_DB}, got {resolved}")


def conn() -> sqlite3.Connection:
    assert_acceptance_db(ACCEPTANCE_DB)
    return get_connection(ACCEPTANCE_DB)


def run_git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=PROJECT_ROOT, text=True).strip()


def run_full_suite(state: AcceptanceState) -> None:
    cmd = [str(INTERPRETER), "-m", "pytest", "tests/", "-v", "--tb=short"]
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, text=True, capture_output=True)
    output = proc.stdout + proc.stderr
    summary = re.search(r"=+\s*(?P<summary>.+?)\s+in\s+[\d.]+s\s*=+", output.splitlines()[-1] if output.splitlines() else "")
    collected = re.search(r"collected\s+(\d+)\s+items", output)
    passed = re.search(r"(\d+)\s+passed", output)
    failed = re.search(r"(\d+)\s+failed", output)
    skipped = re.search(r"(\d+)\s+skipped", output)
    warnings = re.search(r"(\d+)\s+warnings?", output)
    state.full_suite = {
        "command": " ".join(cmd),
        "returncode": proc.returncode,
        "collected": int(collected.group(1)) if collected else None,
        "passed": int(passed.group(1)) if passed else 0,
        "failed": int(failed.group(1)) if failed else 0,
        "skipped": int(skipped.group(1)) if skipped else 0,
        "warnings": int(warnings.group(1)) if warnings else 0,
        "summary": summary.group("summary") if summary else "",
    }
    check(state, "3-full-test-suite", proc.returncode == 0, output[-2000:])


def table_names() -> set[str]:
    with closing(conn()) as c:
        return {row["name"] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def table_count(table: str) -> int:
    with closing(conn()) as c:
        return int(c.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def position(portfolio: str, symbol: str, direction: str = "LONG") -> str | None:
    with closing(conn()) as c:
        row = c.execute(
            """
            SELECT current_allocation_pct FROM positions
            WHERE portfolio_id=? AND symbol=? AND direction=? AND status='OPEN'
            ORDER BY id DESC LIMIT 1
            """,
            (portfolio, symbol, direction),
        ).fetchone()
        return row["current_allocation_pct"] if row else None


def all_rows(table: str) -> list[dict[str, Any]]:
    with closing(conn()) as c:
        return [dict(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]


def row_hash(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = keys or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def setup_database(state: AcceptanceState) -> tuple[int, str]:
    assert_acceptance_db(ACCEPTANCE_DB)
    if ACCEPTANCE_DB.exists():
        ACCEPTANCE_DB.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(ACCEPTANCE_DB) + suffix)
        if sidecar.exists():
            sidecar.unlink()

    init_db(ACCEPTANCE_DB)
    before_tables = table_names()
    before_base_counts = {table: table_count(table) for table in REQUIRED_BASE_TABLES}
    before_default_rows = rows_for_historical_positions()
    before_default_hash = row_hash(before_default_rows)

    apply_migration(ACCEPTANCE_DB)
    apply_migration(ACCEPTANCE_DB)

    names = table_names()
    check(state, "2-migration-base-tables", set(REQUIRED_BASE_TABLES).issubset(names), str(sorted(names)))
    check(state, "2-migration-shadow-tables", set(REQUIRED_SHADOW_TABLES).issubset(names), str(sorted(names)))
    with closing(conn()) as c:
        migration_count = c.execute(
            "SELECT COUNT(*) AS c FROM schema_migrations WHERE migration_name='001_shadow_testing_tables'"
        ).fetchone()["c"]
        fk_enabled = c.execute("PRAGMA foreign_keys").fetchone()[0]
    check(state, "2-migration-idempotency", migration_count == 1, f"migration_count={migration_count}")
    check(state, "2-foreign-keys", fk_enabled == 1, f"foreign_keys={fk_enabled}")
    after_base_counts = {table: table_count(table) for table in REQUIRED_BASE_TABLES}
    check(state, "2-historical-tables-preserved", before_tables.issubset(names), str(sorted(before_tables)))
    check(state, "2-historical-tables-unmodified", before_base_counts == after_base_counts, json.dumps(after_base_counts))
    return len(before_default_rows), before_default_hash


def rows_for_historical_positions() -> list[dict[str, Any]]:
    with closing(conn()) as c:
        return [
            dict(row)
            for row in c.execute(
                "SELECT * FROM positions WHERE portfolio_id IN ('default', 'historical') ORDER BY id"
            ).fetchall()
        ]


def create_initial_positions_csv() -> Path:
    path = STORAGE_DIR / "shadow_acceptance_initial_positions.csv"
    rows = [
        {"symbol": "NIFTY", "direction": "LONG", "current_allocation_pct": "100", "market_group": "Indices"},
        {"symbol": "NVDA", "direction": "LONG", "current_allocation_pct": "75", "market_group": "Equity"},
    ]
    write_csv(path, rows, ["symbol", "direction", "current_allocation_pct", "market_group"])
    return path


def scenario_import(state: AcceptanceState) -> None:
    csv_path = create_initial_positions_csv()
    dry = import_positions(csv_path, ACCEPTANCE_DB, dry_run=True, apply=False, confirm=False)
    check(state, "4-import-dry-run", dry == 0, f"return={dry}")
    check(state, "4-import-dry-run-no-shadow", table_count("positions") == 0, "no writes during dry run")
    applied = import_positions(csv_path, ACCEPTANCE_DB, dry_run=False, apply=True, confirm=True)
    check(state, "4-import-apply", applied == 0, f"return={applied}")
    check(state, "4-import-verified-only", position("verified", "NIFTY") == "100" and position("verified", "NVDA") == "75", "verified positions imported")
    check(state, "4-import-shadow-empty", position("shadow", "NIFTY") is None and position("shadow", "NVDA") is None, "shadow remains empty")
    before_positions = table_count("positions")
    repeated = import_positions(csv_path, ACCEPTANCE_DB, dry_run=False, apply=True, confirm=True)
    check(state, "4-import-overwrite-prevented", repeated == 0 and table_count("positions") == before_positions, "repeat import blocked without overwrite")


def scenario_reductions(state: AcceptanceState) -> None:
    steps: list[dict[str, Any]] = []
    check(state, "5-seed-nifty", position("verified", "NIFTY") == "100", f"verified={position('verified', 'NIFTY')}")

    before = {"verified": position("verified", "NIFTY"), "shadow": position("shadow", "NIFTY")}
    ingest_message("acceptance-001", "SELL 50% NIFTY", "Indices", ACCEPTANCE_DB)
    after_ingest = {"verified": position("verified", "NIFTY"), "shadow": position("shadow", "NIFTY")}
    check(state, "5-first-ingest", after_ingest["verified"] == "100" and after_ingest["shadow"] == "50.0000000000", json.dumps(after_ingest))
    approve_as_parsed("acceptance-001", "AcceptanceReviewer", ACCEPTANCE_DB)
    after_approve = {"verified": position("verified", "NIFTY"), "shadow": position("shadow", "NIFTY")}
    check(state, "5-first-approve", after_approve["verified"] == "50.0000000000" and after_approve["shadow"] == "50.0000000000", json.dumps(after_approve))
    steps.append({"message_id": "acceptance-001", "before": before, "after_ingest": after_ingest, "after_approve": after_approve})

    before = {"verified": position("verified", "NIFTY"), "shadow": position("shadow", "NIFTY")}
    ingest_message("acceptance-002", "SELL 50% NIFTY", "Indices", ACCEPTANCE_DB)
    after_ingest = {"verified": position("verified", "NIFTY"), "shadow": position("shadow", "NIFTY")}
    check(state, "5-second-ingest", after_ingest["verified"] == "50.0000000000" and after_ingest["shadow"] == "25.0000000000", json.dumps(after_ingest))
    approve_as_parsed("acceptance-002", "AcceptanceReviewer", ACCEPTANCE_DB)
    after_approve = {"verified": position("verified", "NIFTY"), "shadow": position("shadow", "NIFTY")}
    check(state, "5-second-approve", after_approve["verified"] == "25.0000000000" and after_approve["shadow"] == "25.0000000000", json.dumps(after_approve))
    steps.append({"message_id": "acceptance-002", "before": before, "after_ingest": after_ingest, "after_approve": after_approve})
    state.scenario_results["nifty_reductions"] = steps


def scenario_revalidation(state: AcceptanceState) -> None:
    ingest_message("acceptance-reval-stale", "SELL 50% NIFTY", "Indices", ACCEPTANCE_DB)
    stale_expected = Decimal(position("verified", "NIFTY")) * Decimal("0.5")
    ingest_message("acceptance-reval-latest", "SELL 20% NIFTY", "Indices", ACCEPTANCE_DB)
    approve_as_parsed("acceptance-reval-latest", "AcceptanceReviewer", ACCEPTANCE_DB)
    latest_before = position("verified", "NIFTY")
    approve_as_parsed("acceptance-reval-stale", "AcceptanceReviewer", ACCEPTANCE_DB)
    actual = position("verified", "NIFTY")
    result = {
        "stale_ingestion_time_expected_value": str(stale_expected.quantize(Decimal("0.0000000001"))),
        "latest_verified_before_original_approval": latest_before,
        "actual_approval_time_verified_value": actual,
    }
    state.scenario_results["approval_time_revalidation"] = result
    check(state, "6-approval-time-revalidation", actual == "10.0000000000", json.dumps(result))


def scenario_edit_reject_context(state: AcceptanceState) -> None:
    ingest_message("acceptance-003", "PART PROFIT BOOK IN NVDA", "Equity", ACCEPTANCE_DB)
    details = get_review_details("acceptance-003", ACCEPTANCE_DB)
    event = ParsedTradeEvent.from_json(details["prediction_json"])
    check(
        state,
        "7-initial-parser-proposal",
        event.final_action == "REDUCE_POSITION" and event.symbol == "NVDA" and str(event.quantity_percent) in {"25", "25.0"} and event.quantity_basis == "CURRENT_HOLDING",
        event.to_json(),
    )
    corrected = ParsedTradeEvent.from_json(event.to_json())
    corrected.quantity_percent = Decimal("50")
    result = edit_and_approve(
        "acceptance-003",
        "AcceptanceReviewer",
        corrected,
        {"quantity_percent": {"from": str(event.quantity_percent), "to": "50"}},
        "Acceptance correction to 50%",
        ACCEPTANCE_DB,
    )
    with closing(conn()) as c:
        pred_qty = c.execute("SELECT quantity_percent FROM parser_predictions WHERE source_message_id='acceptance-003'").fetchone()["quantity_percent"]
        review = dict(c.execute("SELECT decision, changed_fields_json FROM human_reviews WHERE source_message_id='acceptance-003'").fetchone())
        verified_event = c.execute("SELECT approved_event_json FROM verified_events WHERE source_message_id='acceptance-003'").fetchone()["approved_event_json"]
        label = dict(c.execute("SELECT was_corrected FROM verified_labels WHERE source_message_id='acceptance-003'").fetchone())
    edit_result = {
        "service_result": result,
        "verified_nvda": position("verified", "NVDA"),
        "original_prediction_quantity_percent": pred_qty,
        "review": review,
        "verified_event": json.loads(verified_event),
        "label": label,
    }
    state.scenario_results["edit_and_approve"] = edit_result
    check(state, "7-edit-and-approve", result["status"] == "APPROVED" and position("verified", "NVDA") == "37.5000000000" and pred_qty in {"25", "25.0"} and "quantity_percent" in review["changed_fields_json"] and label["was_corrected"] == 1, json.dumps(edit_result, default=str))

    ingest_message("acceptance-004", "BUY 50% AMD @160", "Equity", ACCEPTANCE_DB)
    reject_result = reject("acceptance-004", "AcceptanceReviewer", "Acceptance rejection", ACCEPTANCE_DB)
    with closing(conn()) as c:
        verified_amd_events = c.execute("SELECT COUNT(*) AS c FROM verified_events WHERE source_message_id='acceptance-004'").fetchone()["c"]
        shadow_amd_events = c.execute("SELECT COUNT(*) AS c FROM shadow_events WHERE source_message_id='acceptance-004'").fetchone()["c"]
    state.scenario_results["rejection"] = {
        "service_result": reject_result,
        "verified_amd": position("verified", "AMD"),
        "verified_event_count": verified_amd_events,
        "shadow_event_count": shadow_amd_events,
    }
    check(state, "8-rejection", reject_result["status"] == "REJECTED" and position("verified", "AMD") is None and verified_amd_events == 0 and shadow_amd_events == 1, json.dumps(state.scenario_results["rejection"]))

    ingest_message("acceptance-nontrade", "Market looks calm today", "Commentary", ACCEPTANCE_DB)
    non_trade = mark_non_trade("acceptance-nontrade", "AcceptanceReviewer", "commentary", ACCEPTANCE_DB)
    ingest_message("acceptance-context", "IF NIFTY BREAKS 26000 BUY", "Indices", ACCEPTANCE_DB)
    needs_context = mark_needs_context("acceptance-context", "AcceptanceReviewer", "conditional", ACCEPTANCE_DB)
    state.scenario_results["non_trade_context"] = {"non_trade": non_trade, "needs_context": needs_context}
    check(state, "9-non-trade-needs-context", non_trade["status"] == "NON_TRADE" and needs_context["status"] == "NEEDS_CONTEXT", json.dumps(state.scenario_results["non_trade_context"]))


def scenario_idempotency(state: AcceptanceState) -> None:
    before = counts_snapshot()
    dup_ingest = ingest_message("acceptance-004", "BUY 50% AMD @160", "Equity", ACCEPTANCE_DB)
    dup_approval = approve_as_parsed("acceptance-001", "OtherReviewer", ACCEPTANCE_DB)
    after = counts_snapshot()
    state.scenario_results["idempotency"] = {"duplicate_ingestion": dup_ingest, "duplicate_approval": dup_approval, "before": before, "after": after}
    check(state, "10-idempotency", dup_ingest["status"] == "DUPLICATE" and dup_approval["status"] == "ALREADY_REVIEWED" and before == after, json.dumps(state.scenario_results["idempotency"], default=str))


def counts_snapshot() -> dict[str, int]:
    return {table: table_count(table) for table in ["incoming_messages", "parser_predictions", "shadow_events", "verified_events", "human_reviews", "positions"]}


def scenario_concurrent_restart(state: AcceptanceState) -> None:
    ingest_message("acceptance-concurrent", "BUY 10% MSFT @400", "Equity", ACCEPTANCE_DB)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda reviewer: approve_as_parsed("acceptance-concurrent", reviewer, ACCEPTANCE_DB), ["ReviewerA", "ReviewerB"]))
    statuses = sorted(result["status"] for result in results)
    with closing(conn()) as c:
        event_count = c.execute("SELECT COUNT(*) AS c FROM verified_events WHERE source_message_id='acceptance-concurrent'").fetchone()["c"]
        review_count = c.execute("SELECT COUNT(*) AS c FROM human_reviews WHERE source_message_id='acceptance-concurrent'").fetchone()["c"]
    state.scenario_results["concurrent_review"] = {"results": results, "event_count": event_count, "review_count": review_count}
    check(state, "11-concurrent-approval", statuses == ["ALREADY_REVIEWED", "APPROVED"] and event_count == 1 and review_count == 1, json.dumps(state.scenario_results["concurrent_review"]))

    ingest_message("acceptance-restart", "BUY 5% TSLA @250", "Equity", ACCEPTANCE_DB)
    code = (
        "import json; from pathlib import Path; "
        "from src.shadow_service import get_pending_reviews, get_review_details; "
        "from src.database import get_connection; "
        f"db=Path(r'{ACCEPTANCE_DB}'); "
        "pending=get_pending_reviews(db); details=get_review_details('acceptance-restart', db); "
        "conn=get_connection(db); "
        "positions=[dict(r) for r in conn.execute(\"SELECT portfolio_id,symbol,current_allocation_pct FROM positions WHERE portfolio_id IN ('shadow','verified')\").fetchall()]; conn.close(); "
        "print(json.dumps({'pending_ids':[p['source_message_id'] for p in pending], 'details': bool(details and details.get('prediction_json')), 'positions': positions}))"
    )
    proc = subprocess.run([str(INTERPRETER), "-c", code], cwd=PROJECT_ROOT, text=True, capture_output=True)
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    state.scenario_results["restart_persistence"] = payload
    check(state, "12-restart-persistence", proc.returncode == 0 and "acceptance-restart" in payload["pending_ids"] and payload["details"], proc.stdout + proc.stderr)


def scenario_streamlit(state: AcceptanceState) -> None:
    before = counts_snapshot()
    try:
        from streamlit.testing.v1 import AppTest

        os.environ["TRADE_DB_PATH"] = str(ACCEPTANCE_DB)
        import src.config as config
        import src.shadow_service as shadow_service

        config.DATABASE_PATH = ACCEPTANCE_DB
        shadow_service.DATABASE_PATH = ACCEPTANCE_DB
        app = AppTest.from_file(str(PROJECT_ROOT / "app" / "shadow_review_app.py"))
        app.run(timeout=15)
        queue_loaded = any("Pending Items:" in str(item.value) for item in app.markdown)
        button_count = len(app.button)
        if button_count:
            app.button[0].click().run(timeout=15)
        review_opened = bool(app.json) or any("Review:" in str(item.value) for item in app.markdown)
        app.run(timeout=15)
        after = counts_snapshot()
        state.streamlit = {
            "method": "streamlit.testing.v1.AppTest",
            "queue_loaded": queue_loaded,
            "button_count": button_count,
            "review_opened": review_opened,
            "before_counts": before,
            "after_counts": after,
        }
    except Exception as exc:
        state.streamlit = {"method": "streamlit.testing.v1.AppTest", "error": repr(exc), "before_counts": before, "after_counts": counts_snapshot()}
    check(state, "13-streamlit-smoke", state.streamlit.get("queue_loaded") and state.streamlit.get("review_opened") and state.streamlit["before_counts"] == state.streamlit["after_counts"], json.dumps(state.streamlit, default=str))


def scenario_export_metrics(state: AcceptanceState) -> None:
    export_path = PROJECT_ROOT / "data" / "verified_shadow_labels.csv"
    export_labels(export_path, ACCEPTANCE_DB)
    with export_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    decisions = {row["human decision"] for row in rows}
    required_columns = {
        "reviewer",
        "review timestamp",
        "original ml action",
        "original rule action",
        "final human-reviewed event json",
        "changed fields",
        "parser version",
        "model version",
        "label classification",
    }
    state.scenario_results["export"] = {"path": str(export_path), "count": len(rows), "decisions": sorted(decisions)}
    check(state, "14-human-reviewed-export", len(rows) > 0 and {"APPROVED", "APPROVED_WITH_CORRECTION", "REJECTED", "NON_TRADE"}.issubset(decisions) and required_columns.issubset(rows[0].keys()), json.dumps(state.scenario_results["export"]))

    generate_shadow_metrics(ACCEPTANCE_DB, str(REPORTS_DIR))
    metrics_path = REPORTS_DIR / "shadow_testing_summary.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    required_metrics = {
        "action_accuracy",
        "symbol_accuracy",
        "percentage_accuracy",
        "direction_accuracy",
        "complete_event_accuracy",
        "approval_rate",
        "correction_rate",
        "rejection_rate",
        "manual_review_rate",
        "verified_shadow_position_differences",
    }
    state.scenario_results["shadow_metrics"] = metrics
    check(state, "15-shadow-metrics", required_metrics.issubset(metrics.keys()) and metrics["total_reviews"] > 0, json.dumps(metrics))


def write_reports(state: AcceptanceState, baseline_count: int, baseline_hash: str) -> None:
    after_rows = rows_for_historical_positions()
    after_hash = row_hash(after_rows)
    historical_unchanged = baseline_count == len(after_rows) and baseline_hash == after_hash
    check(state, "16-historical-default-isolation", historical_unchanged, f"before={baseline_count}/{baseline_hash} after={len(after_rows)}/{after_hash}")

    table_count_rows = [{"table": table, "count": table_count(table)} for table in REQUIRED_BASE_TABLES + REQUIRED_SHADOW_TABLES + ["schema_migrations"]]
    write_csv(REPORT_PREFIX.with_name("shadow_acceptance_table_counts.csv"), table_count_rows, ["table", "count"])

    with closing(conn()) as c:
        schema_rows = []
        for table in sorted(table_names()):
            for row in c.execute(f"PRAGMA table_info({table})").fetchall():
                item = dict(row)
                item["table"] = table
                schema_rows.append(item)
    write_csv(REPORT_PREFIX.with_name("shadow_acceptance_schema.csv"), schema_rows)
    write_csv(REPORT_PREFIX.with_name("shadow_acceptance_events.csv"), all_rows("shadow_events") + all_rows("verified_events"))
    write_csv(REPORT_PREFIX.with_name("shadow_acceptance_positions.csv"), all_rows("positions"))
    write_csv(REPORT_PREFIX.with_name("shadow_acceptance_reviews.csv"), all_rows("human_reviews"))

    revalidation_path = REPORT_PREFIX.with_name("shadow_acceptance_revalidation.json")
    revalidation_path.write_text(json.dumps(state.scenario_results["approval_time_revalidation"], indent=2), encoding="utf-8")

    coverage_rows = coverage_matrix_rows(state)
    write_csv(REPORT_PREFIX.with_name("shadow_test_coverage_matrix.csv"), coverage_rows)

    classification = "READY_FOR_MANUAL_SHADOW_USE" if not state.failures else "SHADOW_ACCEPTANCE_FAILED"
    summary = {
        "interpreter_path": str(INTERPRETER),
        "python_version": platform.python_version(),
        "scikit_learn_version": sklearn.__version__,
        "git_branch": run_git(["branch", "--show-current"]),
        "git_commit": run_git(["rev-parse", "HEAD"]),
        "database_path": str(ACCEPTANCE_DB),
        "acceptance_classification": classification,
        "failed_requirements": state.failures,
        "full_suite": state.full_suite,
        "scenario_results": state.scenario_results,
        "streamlit": state.streamlit,
        "historical_default_positions": {
            "baseline_count": baseline_count,
            "baseline_hash": baseline_hash,
            "final_count": len(after_rows),
            "final_hash": after_hash,
            "changed": not historical_unchanged,
        },
        "defects_fixed": state.defects_fixed,
        "table_counts": table_count_rows,
    }

    json_path = REPORT_PREFIX.with_name("shadow_acceptance_report.json")
    json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    md_path = REPORT_PREFIX.with_name("shadow_acceptance_report.md")
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    for path in [
        md_path,
        json_path,
        REPORT_PREFIX.with_name("shadow_test_coverage_matrix.csv"),
        REPORT_PREFIX.with_name("shadow_acceptance_schema.csv"),
        REPORT_PREFIX.with_name("shadow_acceptance_table_counts.csv"),
        REPORT_PREFIX.with_name("shadow_acceptance_events.csv"),
        REPORT_PREFIX.with_name("shadow_acceptance_positions.csv"),
        REPORT_PREFIX.with_name("shadow_acceptance_reviews.csv"),
        revalidation_path,
        REPORTS_DIR / "shadow_testing_summary.json",
        REPORTS_DIR / "shadow_testing_summary.csv",
        REPORTS_DIR / "shadow_action_metrics.csv",
        REPORTS_DIR / "shadow_entity_metrics.csv",
        REPORTS_DIR / "shadow_review_reason_counts.csv",
        REPORTS_DIR / "shadow_false_automatic_events.csv",
        REPORTS_DIR / "shadow_position_differences.csv",
        PROJECT_ROOT / "data" / "verified_shadow_labels.csv",
    ]:
        if path.exists():
            state.report_artifacts.append(str(path))


def coverage_matrix_rows(state: AcceptanceState) -> list[dict[str, Any]]:
    mapping = [
        ("shadow does not modify verified automatically", "test_shadow_prediction_does_not_change_verified", "tests/test_shadow_workflow.py", "acceptance-001", "shadow_acceptance_report.json"),
        ("approval updates verified", "test_approving_prediction_updates_verified", "tests/test_shadow_workflow.py", "acceptance-001", "shadow_acceptance_report.json"),
        ("edit-and-approve applies corrected values", "test_edit_and_approve_applies_correction", "tests/test_shadow_workflow.py", "acceptance-003", "shadow_acceptance_reviews.csv"),
        ("rejection leaves verified unchanged", "test_rejection_does_not_change_verified", "tests/test_shadow_workflow.py", "acceptance-004", "shadow_acceptance_reviews.csv"),
        ("duplicate ingestion prevention", "test_duplicate_ingestion_ignored", "tests/test_shadow_workflow.py", "acceptance-004", "shadow_acceptance_report.json"),
        ("duplicate approval prevention", "test_duplicate_review_decision_prevented", "tests/test_shadow_workflow.py", "acceptance-001", "shadow_acceptance_report.json"),
        ("shadow and verified event coexistence", "test_same_message_in_shadow_and_verified_events", "tests/test_shadow_workflow.py", "acceptance-001", "shadow_acceptance_events.csv"),
        ("prediction immutability", "test_edit_and_approve_applies_correction", "tests/test_shadow_workflow.py", "acceptance-003", "shadow_acceptance_events.csv"),
        ("latest verified context at approval", "test_approval_uses_latest_verified_context", "tests/test_shadow_workflow.py", "acceptance-reval-stale", "shadow_acceptance_revalidation.json"),
        ("percentage reduction correctness", "TestReductionChain::test_chain_100_to_50_to_25_to_12_5", "tests/test_holdings_engine.py", "acceptance-001 acceptance-002", "shadow_acceptance_report.json"),
        ("restart persistence", "acceptance_harness_restart_persistence", "scripts/run_shadow_acceptance.py", "acceptance-restart", "shadow_acceptance_report.json"),
        ("migration idempotency", "test_migration_is_idempotent", "tests/test_shadow_workflow.py", "migration", "shadow_acceptance_schema.csv"),
        ("foreign-key enforcement", "test_foreign_keys_active", "tests/test_shadow_workflow.py", "migration", "shadow_acceptance_schema.csv"),
        ("historical portfolio isolation", "acceptance_harness_historical_hash", "scripts/run_shadow_acceptance.py", "all", "shadow_acceptance_report.json"),
        ("missing-position reduction blocking", "test_plain_reduction_with_no_verified_holding_blocked", "tests/test_shadow_workflow.py", "bad_red", "pytest"),
        ("verified import overwrite prevention", "acceptance_harness_import", "scripts/run_shadow_acceptance.py", "initial import", "shadow_acceptance_report.json"),
        ("corrected-label export", "test_rejected_and_needs_context_reviews_export_as_training_candidates", "tests/test_shadow_workflow.py", "acceptance-003", "verified_shadow_labels.csv"),
        ("concurrent approval safety", "acceptance_harness_concurrent_review", "scripts/run_shadow_acceptance.py", "acceptance-concurrent", "shadow_acceptance_report.json"),
        ("Streamlit rerun safety", "acceptance_harness_streamlit_smoke", "scripts/run_shadow_acceptance.py", "streamlit", "shadow_acceptance_report.json"),
    ]
    return [
        {
            "requirement": requirement,
            "test_name": test_name,
            "implementation_file": implementation_file,
            "scenario_ids": scenario_ids,
            "result": "PASS" if not state.failures else "FAIL",
            "related_report_artifact": artifact,
        }
        for requirement, test_name, implementation_file, scenario_ids, artifact in mapping
    ]


def render_markdown(summary: dict[str, Any]) -> str:
    reductions = summary["scenario_results"].get("nifty_reductions", [])
    lines = [
        "# Shadow Acceptance Report",
        "",
        f"- Classification: `{summary['acceptance_classification']}`",
        f"- Interpreter: `{summary['interpreter_path']}`",
        f"- Python: `{summary['python_version']}`",
        f"- scikit-learn: `{summary['scikit_learn_version']}`",
        f"- Git: `{summary['git_branch']}` `{summary['git_commit']}`",
        f"- Database: `{summary['database_path']}`",
        f"- Full suite: `{summary['full_suite'].get('passed')}` passed, `{summary['full_suite'].get('failed')}` failed, `{summary['full_suite'].get('skipped')}` skipped, `{summary['full_suite'].get('warnings')}` warnings",
        "",
        "## Key Scenario Results",
        "",
        f"- First NIFTY reduction: `{reductions[0] if reductions else None}`",
        f"- Second NIFTY reduction: `{reductions[1] if len(reductions) > 1 else None}`",
        f"- Approval-time revalidation: `{summary['scenario_results'].get('approval_time_revalidation')}`",
        f"- Edit and approve: `{summary['scenario_results'].get('edit_and_approve')}`",
        f"- Rejection: `{summary['scenario_results'].get('rejection')}`",
        f"- Idempotency: `{summary['scenario_results'].get('idempotency')}`",
        f"- Concurrent review: `{summary['scenario_results'].get('concurrent_review')}`",
        f"- Restart persistence: `{summary['scenario_results'].get('restart_persistence')}`",
        f"- Streamlit smoke: `{summary.get('streamlit')}`",
        f"- Export: `{summary['scenario_results'].get('export')}`",
        f"- Metrics: `{summary['scenario_results'].get('shadow_metrics')}`",
        "",
        "## Defects Fixed",
    ]
    for defect in summary["defects_fixed"]:
        lines.append(f"- {defect['scenario']}: {defect['root_cause']} Fix: {defect['fix']}. Test: {defect['regression_test']}.")
    lines.extend([
        "",
        "## Historical Isolation",
        "",
        f"- Changed: `{summary['historical_default_positions']['changed']}`",
        f"- Baseline count/hash: `{summary['historical_default_positions']['baseline_count']}` / `{summary['historical_default_positions']['baseline_hash']}`",
        f"- Final count/hash: `{summary['historical_default_positions']['final_count']}` / `{summary['historical_default_positions']['final_hash']}`",
        "",
        "## Unresolved Issues",
        "",
        "- None." if not summary["failed_requirements"] else "\n".join(f"- {item}" for item in summary["failed_requirements"]),
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    state = AcceptanceState()
    state.defects_fixed = [
        {
            "scenario": "4 verified import / 2 migration foreign keys",
            "root_cause": "Shadow migration and import/export tools were hard-wired to the default database, and import setup events lacked parent incoming_messages rows.",
            "fix": "Added explicit database path support and import source rows for verified setup events.",
            "regression_test": "tests/test_shadow_workflow.py::test_shadow_reduction_uses_verified_baseline_when_shadow_empty",
        },
        {
            "scenario": "5 current-holding percentage workflow",
            "root_cause": "Shadow reduction simulation could not reduce an imported verified position because the shadow portfolio was intentionally empty after import.",
            "fix": "Seeded a shadow-only baseline copy from verified holdings when applying safe reduction predictions to shadow.",
            "regression_test": "tests/test_shadow_workflow.py::test_shadow_reduction_uses_verified_baseline_when_shadow_empty",
        },
        {
            "scenario": "14 human-reviewed data export",
            "root_cause": "Rejected and needs-context decisions were not exported as human-reviewed training candidates, and export omitted required metadata.",
            "fix": "Recorded candidate labels for non-approved human decisions and expanded export columns.",
            "regression_test": "tests/test_shadow_workflow.py::test_rejected_and_needs_context_reviews_export_as_training_candidates",
        },
    ]
    baseline_count, baseline_hash = setup_database(state)
    run_full_suite(state)
    scenario_import(state)
    scenario_reductions(state)
    scenario_revalidation(state)
    scenario_edit_reject_context(state)
    scenario_idempotency(state)
    scenario_concurrent_restart(state)
    scenario_streamlit(state)
    scenario_export_metrics(state)
    write_reports(state, baseline_count, baseline_hash)
    print(json.dumps({"classification": "READY_FOR_MANUAL_SHADOW_USE", "reports": state.report_artifacts}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"SHADOW_ACCEPTANCE_FAILED: {exc}", file=sys.stderr)
        raise
