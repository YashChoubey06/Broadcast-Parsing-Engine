import csv
import hashlib
import json
from pathlib import Path

from scripts.phase4_reversal_candidate_scanner import CANDIDATE_FIELDS
from src import phase4_review as review
from src.config import MODEL_PATH
from src.phase5_training_export import (
    EXPORT_FIELDS,
    Phase5Paths,
    assert_expected_audit,
    audit_review_decisions,
    evaluate_parser_baseline,
    export_labels,
)


def _child_json(
    *,
    text,
    surface,
    symbol="ABC",
    position_effect="CLOSE",
    side="",
    entry_capacity_pct=None,
    stop_loss=None,
    targets=None,
):
    return json.dumps(
        {
            "clause_text": text,
            "surface_instruction": surface,
            "symbol": symbol,
            "contract_month": "",
            "market_group": "NSE",
            "position_effect": position_effect,
            "resolved_position_side": side,
            "direction": side,
            "entry_capacity_pct": entry_capacity_pct,
            "stop_loss": stop_loss,
            "targets": targets or [],
        },
        sort_keys=True,
    )


def _candidate(candidate_id, status=review.PENDING_REVIEW, **overrides):
    clause_1 = "FULL PROFIT BOOK IN ABC"
    clause_2 = "50% SELL ABC @100 SL 110 TGT 90-80"
    base = {field: "" for field in CANDIDATE_FIELDS}
    base.update(
        {
            "candidate_id": candidate_id,
            "source_file": "local-test.csv",
            "source_sha256": "test",
            "source_row_number": "1",
            "source_message_id": f"msg-{candidate_id}",
            "segment_name": "NSE",
            "raw_text": f"{clause_1} & {clause_2}",
            "normalized_text": f"{clause_1} & {clause_2}",
            "candidate_kind": "STRICT_ORDERED_REVERSAL",
            "actionable_clause_count": "2",
            "clause_1_text": clause_1,
            "clause_2_text": clause_2,
            "phase3_bundle_type": "ORDERED_REVERSAL",
            "phase3_is_ordered": "true",
            "phase3_child_count": "2",
            "phase3_needs_review": "true",
            "child_1_json": _child_json(text=clause_1, surface="BOOK_FULL"),
            "child_2_json": _child_json(
                text=clause_2,
                surface="SELL",
                position_effect="OPEN",
                side="SHORT",
                entry_capacity_pct="50",
                stop_loss="110",
                targets=["90", "80"],
            ),
            "child_1_surface_instruction": "BOOK_FULL",
            "child_2_surface_instruction": "SELL",
            "child_1_symbol": "ABC",
            "child_2_symbol": "ABC",
            "child_1_position_effect": "CLOSE",
            "child_2_position_effect": "OPEN",
            "child_2_entry_capacity_pct": "50",
            "same_instrument_identity": "true",
            "review_status": status,
            "use_for_training": "false",
        }
    )
    base.update(overrides)
    return base


def _decision(
    candidate_id,
    status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
    *,
    structure=True,
    portfolio=False,
    portfolio_status=review.NOT_EVALUATED,
    reviewer="Analyst",
    reviewed_at="2026-07-06T00:00:00Z",
    legacy=None,
    **overrides,
):
    base = {field: "" for field in review.DECISION_FIELDS}
    base.update(
        {
            "candidate_id": candidate_id,
            "review_status": status,
            "use_for_training": "true" if (structure if legacy is None else legacy) else "false",
            "use_for_structure_training": "true" if structure else "false",
            "use_for_portfolio_effect_training": "true" if portfolio else "false",
            "portfolio_effect_status": portfolio_status,
            "review_bundle_type": review.ORDERED_CLOSE_THEN_ENTRY
            if status == review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY
            else "",
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "decision_revision": "1",
            "changed_fields_json": "{}",
        }
    )
    base.update(overrides)
    return base


def _write_csv(path: Path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path):
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _hash(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _paths(tmp_path, candidates, decisions):
    candidate_csv = tmp_path / "phase4_reversal_candidates.csv"
    decisions_csv = tmp_path / "phase4_reversal_review_decisions.csv"
    reviewed_csv = tmp_path / "phase4_reversal_candidates_reviewed.csv"
    reports_dir = tmp_path / "reports"
    label_csv = tmp_path / "data" / "derived" / "labels.csv"
    label_jsonl = tmp_path / "data" / "derived" / "labels.jsonl"

    _write_csv(candidate_csv, candidates, CANDIDATE_FIELDS)
    _write_csv(decisions_csv, decisions, review.DECISION_FIELDS)
    by_decision = {row["candidate_id"]: row for row in decisions}
    merged_fields = list(CANDIDATE_FIELDS) + [
        field for field in review.DECISION_FIELDS if field not in CANDIDATE_FIELDS
    ]
    merged = []
    for candidate in candidates:
        row = dict(candidate)
        decision = review.DEFAULT_DECISION.copy()
        decision["candidate_id"] = candidate["candidate_id"]
        decision.update(by_decision.get(candidate["candidate_id"], {}))
        row.update(decision)
        merged.append(row)
    _write_csv(reviewed_csv, merged, merged_fields)

    return Phase5Paths(
        candidate_csv=candidate_csv,
        decisions_csv=decisions_csv,
        reviewed_csv=reviewed_csv,
        label_csv=label_csv,
        label_jsonl=label_jsonl,
        reports_dir=reports_dir,
    )


def _standard_paths(tmp_path):
    candidates = [
        _candidate("eligible"),
        _candidate("multi"),
        _candidate("false"),
        _candidate("context"),
        _candidate("do-not-use"),
    ]
    decisions = [
        _decision("eligible"),
        _decision("multi", review.CONFIRMED_MULTI_CLAUSE_REVIEW_ONLY, structure=False, legacy=False),
        _decision("false", review.FALSE_POSITIVE, structure=False, legacy=False),
        _decision("context", review.NEEDS_MORE_CONTEXT, structure=False, legacy=False),
        _decision("do-not-use", review.DO_NOT_USE_FOR_TRAINING, structure=False, legacy=False),
    ]
    return _paths(tmp_path, candidates, decisions)


def test_audit_detects_pending_candidates(tmp_path):
    paths = _paths(tmp_path, [_candidate("pending")], [])
    audit = audit_review_decisions(paths)

    assert audit["failures"]["pending_candidate_ids"] == ["pending"]


def test_audit_detects_missing_reviewer_and_reviewed_at(tmp_path):
    paths = _paths(
        tmp_path,
        [_candidate("missing")],
        [_decision("missing", reviewer="", reviewed_at="")],
    )
    audit = audit_review_decisions(paths)

    assert audit["failures"]["missing_reviewer_ids"] == ["missing"]
    assert audit["failures"]["missing_reviewed_at_ids"] == ["missing"]


def test_audit_detects_duplicate_and_missing_decision_ids(tmp_path):
    paths = _paths(
        tmp_path,
        [_candidate("known")],
        [_decision("known"), _decision("known"), _decision("unknown")],
    )
    audit = audit_review_decisions(paths)

    assert audit["failures"]["duplicate_decision_ids"] == ["known"]
    assert audit["failures"]["decision_ids_missing_from_candidate_csv"] == ["unknown"]


def test_audit_detects_bad_training_flags(tmp_path):
    paths = _paths(
        tmp_path,
        [_candidate("bad-structure"), _candidate("bad-portfolio")],
        [
            _decision("bad-structure", review.FALSE_POSITIVE, structure=True),
            _decision("bad-portfolio", portfolio=True, portfolio_status=review.NOT_EVALUATED),
        ],
    )
    audit = audit_review_decisions(paths)

    assert audit["failures"]["structure_flag_bad_status_ids"] == ["bad-structure"]
    assert audit["failures"]["portfolio_flag_not_evaluated_ids"] == ["bad-portfolio"]
    assert audit["failures"]["portfolio_flag_bad_status_ids"] == ["bad-portfolio"]


def test_legacy_use_for_training_maps_only_to_structure_training(tmp_path):
    paths = _paths(
        tmp_path,
        [_candidate("legacy")],
        [_decision("legacy", legacy=True, structure=True, portfolio=False)],
    )
    audit = audit_review_decisions(paths)

    assert audit["legacy_use_for_training_true_count"] == 1
    assert audit["mapped_to_structure_training_count"] == 1
    assert audit["mapped_to_portfolio_effect_training_count"] == 0


def test_export_includes_exactly_eligible_and_excludes_disallowed_statuses(tmp_path):
    paths = _standard_paths(tmp_path)

    rows, summary = export_labels(paths, enforce_expected=False)

    assert [row["candidate_id"] for row in rows] == ["eligible"]
    assert summary["export_count"] == 1
    assert rows[0]["label_type"] == "ORDERED_CLOSE_THEN_ENTRY"


def test_corrected_fields_override_parser_fields(tmp_path):
    paths = _paths(
        tmp_path,
        [_candidate("corrected")],
        [
            _decision(
                "corrected",
                corrected_child_2_symbol="XYZ",
                corrected_clause_2_text="50% SELL XYZ",
                changed_fields_json='{"child_2_symbol":{"original":"ABC","corrected":"XYZ"}}',
            )
        ],
    )

    rows, _summary = export_labels(paths, enforce_expected=False)

    assert rows[0]["child_2_symbol"] == "XYZ"
    assert rows[0]["clause_2_text"] == "50% SELL XYZ"
    assert json.loads(rows[0]["changed_fields_json"])["child_2_symbol"]["corrected"] == "XYZ"


def test_missing_parser_child_uses_reviewed_text_fallback_labels(tmp_path):
    raw_text = "Exit from crude and sell @ 84.20 with SL 86.00 for target 80.50-78.00"
    paths = _paths(
        tmp_path,
        [
            _candidate(
                "fallback",
                raw_text=raw_text,
                normalized_text=raw_text,
                clause_1_text=raw_text,
                clause_2_text="",
                phase3_bundle_type="SINGLE",
                phase3_is_ordered="false",
                phase3_child_count="1",
                child_1_json=_child_json(
                    text=raw_text,
                    surface="EXIT",
                    symbol="CRUDE",
                    position_effect="CLOSE",
                    stop_loss="86.00",
                    targets=["80.50", "78.00"],
                ),
                child_2_json="",
                child_1_surface_instruction="EXIT",
                child_1_symbol="CRUDE",
                child_1_position_effect="CLOSE",
                child_2_surface_instruction="",
                child_2_symbol="",
                child_2_position_effect="",
                same_instrument_identity="",
            )
        ],
        [_decision("fallback")],
    )

    rows, _summary = export_labels(paths, enforce_expected=False)

    assert rows[0]["clause_1_text"] == "Exit from crude"
    assert rows[0]["clause_2_text"].lower().startswith("sell @ 84.20")
    assert rows[0]["child_2_symbol"] == "CRUDE"
    assert rows[0]["child_2_surface_instruction"] == "SELL"
    assert rows[0]["child_2_requested_side"] == "SHORT"
    assert rows[0]["child_2_stop_loss"] == "86"
    assert rows[0]["child_2_targets"] == '["80.5","78"]'


def test_export_does_not_modify_candidate_or_decision_csv_and_is_deterministic(tmp_path):
    paths = _standard_paths(tmp_path)
    candidate_before = _hash(paths.candidate_csv)
    decision_before = _hash(paths.decisions_csv)

    export_labels(paths, enforce_expected=False)
    csv_hash_1 = _hash(paths.label_csv)
    jsonl_hash_1 = _hash(paths.label_jsonl)
    export_labels(paths, enforce_expected=False)

    assert _hash(paths.candidate_csv) == candidate_before
    assert _hash(paths.decisions_csv) == decision_before
    assert _hash(paths.label_csv) == csv_hash_1
    assert _hash(paths.label_jsonl) == jsonl_hash_1


def test_parser_baseline_evaluation_produces_field_level_results(tmp_path):
    paths = _standard_paths(tmp_path)
    rows, _summary = export_labels(paths, enforce_expected=False)

    evaluation, failures = evaluate_parser_baseline(rows, paths)

    assert evaluation["label_count"] == 1
    assert set(evaluation["field_metrics"]) >= {
        "ordered_close_then_entry_detection",
        "clause_split_accuracy",
        "entry_clause_targets",
    }
    assert isinstance(failures, list)


def test_reports_do_not_include_full_raw_message_text(tmp_path):
    paths = _standard_paths(tmp_path)
    rows, _summary = export_labels(paths, enforce_expected=False)
    evaluate_parser_baseline(rows, paths)

    raw_text = _read_csv(paths.label_csv)[0]["raw_text"]
    for report_path in paths.reports_dir.iterdir():
        assert raw_text not in report_path.read_text(encoding="utf-8")


def test_no_production_model_artifact_is_modified(tmp_path):
    paths = _standard_paths(tmp_path)
    before = _hash(MODEL_PATH)

    rows, _summary = export_labels(paths, enforce_expected=False)
    evaluate_parser_baseline(rows, paths)

    assert _hash(MODEL_PATH) == before


def test_expected_audit_reports_differences_without_exporting(tmp_path):
    paths = _standard_paths(tmp_path)
    audit = audit_review_decisions(paths)

    try:
        assert_expected_audit(audit)
    except Exception as exc:
        assert "Phase 5 audit does not match expected baseline" in str(exc)
    else:
        raise AssertionError("expected audit baseline mismatch")
