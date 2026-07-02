import csv
import hashlib
import json

import pytest

from scripts.phase4_reversal_candidate_scanner import (
    SourceIntegrityError,
    run_scan,
    verify_source_integrity,
)


HEADERS = ["_id", "segment", "segmentName", "text", "createdAt", "updatedAt"]


def write_fixture(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            base = {
                "_id": "",
                "segment": "seg-1",
                "segmentName": "NSE",
                "text": "",
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
            }
            base.update(row)
            writer.writerow(base)


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scan_fixture(tmp_path, rows):
    source = tmp_path / "raw.csv"
    write_fixture(source, rows)
    before_hash = file_hash(source)
    out_dir = tmp_path / "out"
    totals = run_scan(
        source_path=source,
        candidate_csv=out_dir / "phase4_reversal_candidates.csv",
        manifest_json=out_dir / "phase4_reversal_candidates_manifest.json",
        reports_dir=out_dir / "reports",
        expected_sha256=before_hash,
        expected_rows=len(rows),
    )
    with (out_dir / "phase4_reversal_candidates.csv").open("r", encoding="utf-8", newline="") as f:
        candidate_rows = list(csv.DictReader(f))
    return source, out_dir, totals, candidate_rows


def test_expected_source_hash_verification(tmp_path):
    source = tmp_path / "raw.csv"
    write_fixture(source, [{"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"}])

    integrity = verify_source_integrity(
        source,
        expected_sha256=file_hash(source),
        expected_rows=1,
    )

    assert integrity.data_rows == 1
    assert integrity.source_sha256 == file_hash(source)


def test_hash_mismatch_fails_closed_without_outputs(tmp_path):
    source = tmp_path / "raw.csv"
    write_fixture(source, [{"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"}])
    candidate_csv = tmp_path / "out" / "candidate.csv"

    with pytest.raises(SourceIntegrityError):
        run_scan(
            source_path=source,
            candidate_csv=candidate_csv,
            manifest_json=tmp_path / "out" / "manifest.json",
            reports_dir=tmp_path / "out" / "reports",
            expected_sha256="0" * 64,
            expected_rows=1,
        )

    assert not candidate_csv.exists()


def test_row_count_mismatch_fails_closed_without_outputs(tmp_path):
    source = tmp_path / "raw.csv"
    write_fixture(source, [{"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"}])
    candidate_csv = tmp_path / "out" / "candidate.csv"

    with pytest.raises(SourceIntegrityError):
        run_scan(
            source_path=source,
            candidate_csv=candidate_csv,
            manifest_json=tmp_path / "out" / "manifest.json",
            reports_dir=tmp_path / "out" / "reports",
            expected_sha256=file_hash(source),
            expected_rows=2,
        )

    assert not candidate_csv.exists()


def test_strict_close_plus_entry_detection(tmp_path):
    _source, _out_dir, totals, rows = scan_fixture(
        tmp_path,
        [{"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA @1124 & SELL 50% TSLA @1124"}],
    )

    assert totals.strict_ordered_reversal_count == 1
    assert rows[0]["candidate_kind"] == "STRICT_ORDERED_REVERSAL"
    assert "strict_close_plus_entry" in rows[0]["detection_reasons"]


def test_prose_reversal_detection(tmp_path):
    _source, _out_dir, totals, rows = scan_fixture(
        tmp_path,
        [{"_id": "m1", "text": "Exit from short positions in indices. Buy 50% NASDAQ @29980"}],
    )

    assert totals.prose_reversal_count == 1
    assert rows[0]["candidate_kind"] == "PROSE_REVERSAL_CANDIDATE"


def test_more_than_two_actionable_clauses(tmp_path):
    _source, _out_dir, _totals, rows = scan_fixture(
        tmp_path,
        [{"_id": "m1", "text": "EXIT FROM TSLA & SELL 50% TSLA & BUY 50% TSLA"}],
    )

    assert rows[0]["candidate_kind"] == "TOO_MANY_ACTIONABLE_CLAUSES"
    assert rows[0]["actionable_clause_count"] == "3"


def test_multi_instrument_candidates(tmp_path):
    _source, _out_dir, _totals, rows = scan_fixture(
        tmp_path,
        [{"_id": "m1", "text": "SL touched in Gold & Silver"}],
    )

    assert rows[0]["candidate_kind"] == "MULTI_INSTRUMENT_CANDIDATE"


def test_false_positive_ampersands_are_preserved_for_review(tmp_path):
    _source, _out_dir, totals, rows = scan_fixture(
        tmp_path,
        [{"_id": "m1", "text": "BUY TSLA & wait for confirmation"}],
    )

    assert totals.false_positive_candidate_count == 1
    assert rows[0]["candidate_kind"] == "STRUCTURAL_FALSE_POSITIVE_CANDIDATE"


def test_target_ranges_are_not_split(tmp_path):
    _source, _out_dir, totals, rows = scan_fixture(
        tmp_path,
        [{"_id": "m1", "text": "SELL 50% TSLA @1124 SL 1200 TGT 1100-1050"}],
    )

    assert totals.total_unique_candidate_count == 0
    assert rows == []


def test_deterministic_ids(tmp_path):
    rows = [{"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"}]
    _source1, _out1, _totals1, candidate_rows1 = scan_fixture(tmp_path / "a", rows)
    _source2, _out2, _totals2, candidate_rows2 = scan_fixture(tmp_path / "b", rows)

    assert candidate_rows1[0]["candidate_id"] == candidate_rows2[0]["candidate_id"]


def test_byte_identical_repeated_output(tmp_path):
    rows = [
        {"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"},
        {"_id": "m2", "text": "Exit from short positions in indices. Buy 50% NASDAQ @29980"},
    ]
    source, out_dir, _totals1, _rows1 = scan_fixture(tmp_path, rows)
    csv_bytes_1 = (out_dir / "phase4_reversal_candidates.csv").read_bytes()
    manifest_bytes_1 = (out_dir / "phase4_reversal_candidates_manifest.json").read_bytes()

    source_hash = file_hash(source)
    run_scan(
        source_path=source,
        candidate_csv=out_dir / "phase4_reversal_candidates.csv",
        manifest_json=out_dir / "phase4_reversal_candidates_manifest.json",
        reports_dir=out_dir / "reports",
        expected_sha256=source_hash,
        expected_rows=len(rows),
    )

    assert csv_bytes_1 == (out_dir / "phase4_reversal_candidates.csv").read_bytes()
    assert manifest_bytes_1 == (out_dir / "phase4_reversal_candidates_manifest.json").read_bytes()


def test_source_hash_unchanged_after_scan(tmp_path):
    source, _out_dir, totals, _rows = scan_fixture(
        tmp_path,
        [{"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"}],
    )

    assert totals.source_sha256_before == totals.source_sha256_after == file_hash(source)


def test_phase3_bundle_comparison_fields(tmp_path):
    _source, _out_dir, totals, rows = scan_fixture(
        tmp_path,
        [{"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"}],
    )

    assert totals.phase3_parser_ordered_bundle_count == 1
    assert rows[0]["phase3_bundle_type"] == "ORDERED_REVERSAL"
    assert rows[0]["phase3_child_count"] == "2"
    assert rows[0]["child_1_json"]
    assert rows[0]["child_2_json"]


def test_missing_holdings_context_does_not_discard_structural_candidate(tmp_path):
    _source, _out_dir, _totals, rows = scan_fixture(
        tmp_path,
        [{"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"}],
    )

    assert rows[0]["context_validation_not_run"] == "true"
    assert rows[0]["context_required"] == "true"
    assert rows[0]["candidate_kind"] == "STRICT_ORDERED_REVERSAL"


def test_no_existing_cleaned_or_labelled_dataset_is_overwritten(tmp_path):
    production = tmp_path / "data" / "01_production_messages_final.csv"
    labelled = tmp_path / "data" / "03_model_train_ready_deduplicated.csv"
    production.parent.mkdir()
    production.write_text("keep-production\n", encoding="utf-8")
    labelled.write_text("keep-labels\n", encoding="utf-8")

    scan_fixture(
        tmp_path,
        [{"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"}],
    )

    assert production.read_text(encoding="utf-8") == "keep-production\n"
    assert labelled.read_text(encoding="utf-8") == "keep-labels\n"


def test_all_generated_candidates_default_to_pending_review_and_not_training(tmp_path):
    _source, _out_dir, _totals, rows = scan_fixture(
        tmp_path,
        [
            {"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"},
            {"_id": "m2", "text": "BUY TSLA & wait for confirmation"},
        ],
    )

    assert rows
    assert {row["review_status"] for row in rows} == {"PENDING_REVIEW"}
    assert {row["use_for_training"] for row in rows} == {"false"}


def test_reports_do_not_contain_full_raw_message_text(tmp_path):
    raw = "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"
    _source, out_dir, _totals, _rows = scan_fixture(tmp_path, [{"_id": "m1", "text": raw}])

    for report_name in [
        "phase4_reversal_candidate_summary.json",
        "phase4_reversal_candidate_reason_counts.csv",
        "phase4_reversal_parser_comparison.csv",
        "phase4_reversal_source_integrity.json",
    ]:
        assert raw not in (out_dir / "reports" / report_name).read_text(encoding="utf-8")


def test_manifest_has_canonical_json(tmp_path):
    _source, out_dir, _totals, _rows = scan_fixture(
        tmp_path,
        [{"_id": "m1", "text": "FULL PROFIT BOOK IN TSLA & SELL 50% TSLA"}],
    )

    loaded = json.loads((out_dir / "phase4_reversal_candidates_manifest.json").read_text(encoding="utf-8"))
    assert loaded["context_validation_not_run"] is True
