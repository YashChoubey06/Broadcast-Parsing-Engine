import csv
import hashlib
import json

import pytest

from scripts.phase4_reversal_candidate_scanner import CANDIDATE_FIELDS
from src import phase4_review as review


def write_candidates(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CANDIDATE_FIELDS, lineterminator="\n")
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            base = {field: "" for field in CANDIDATE_FIELDS}
            base.update(
                {
                    "candidate_id": f"rev4_{index}",
                    "source_row_number": str(index),
                    "source_message_id": f"msg-{index}",
                    "segment_name": "NSE",
                    "raw_text": f"raw text {index}",
                    "normalized_text": f"RAW TEXT {index}",
                    "candidate_kind": "STRICT_ORDERED_REVERSAL",
                    "phase3_bundle_type": "ORDERED_REVERSAL",
                    "phase3_is_ordered": "true",
                    "phase3_needs_review": "true",
                    "phase3_child_count": "2",
                    "child_1_position_effect": "CLOSE",
                    "child_2_position_effect": "OPEN",
                    "child_2_surface_instruction": "SELL",
                    "same_instrument_identity": "true",
                    "review_status": review.PENDING_REVIEW,
                    "use_for_training": "false",
                }
            )
            base.update(row)
            writer.writerow(base)


def read_csv(path):
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def review_paths(tmp_path):
    candidate_csv = tmp_path / "phase4_reversal_candidates.csv"
    decisions_csv = tmp_path / "phase4_reversal_review_decisions.csv"
    merged_csv = tmp_path / "phase4_reversal_candidates_reviewed.csv"
    reports_dir = tmp_path / "reports"
    write_candidates(
        candidate_csv,
        [
            {
                "candidate_id": "rev4_strict",
                "source_row_number": "10",
                "raw_text": "FULL PROFIT BOOK IN ABC & SELL 50% ABC",
                "candidate_kind": "STRICT_ORDERED_REVERSAL",
                "phase3_bundle_type": "ORDERED_REVERSAL",
                "phase3_is_ordered": "true",
                "child_2_surface_instruction": "SELL",
            },
            {
                "candidate_id": "rev4_buy",
                "source_row_number": "15",
                "raw_text": "FULL PROFIT BOOK IN ABC & BUY 50% ABC",
                "candidate_kind": "STRICT_ORDERED_REVERSAL",
                "phase3_bundle_type": "ORDERED_REVERSAL",
                "phase3_is_ordered": "true",
                "child_2_surface_instruction": "BUY",
            },
            {
                "candidate_id": "rev4_prose",
                "source_row_number": "20",
                "raw_text": "Exit from positions. Buy ABC",
                "candidate_kind": "PROSE_REVERSAL_CANDIDATE",
                "phase3_bundle_type": "SINGLE",
                "phase3_is_ordered": "false",
            },
            {
                "candidate_id": "rev4_false",
                "source_row_number": "30",
                "raw_text": "BUY ABC & wait",
                "candidate_kind": "STRUCTURAL_FALSE_POSITIVE_CANDIDATE",
                "phase3_bundle_type": "SINGLE",
                "phase3_is_ordered": "false",
            },
        ],
    )
    return candidate_csv, decisions_csv, merged_csv, reports_dir


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_immutable_candidate_file_is_never_modified_and_decision_file_is_separate(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    before = file_hash(candidate_csv)

    review.save_decision(
        "rev4_strict",
        review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
        use_for_structure_training=True,
        reviewer="Analyst",
        candidate_csv=candidate_csv,
        decisions_csv=decisions_csv,
        merged_csv=merged_csv,
    )

    assert file_hash(candidate_csv) == before
    assert decisions_csv.exists()
    assert merged_csv.exists()


def test_decisions_update_by_candidate_id_without_duplication_and_are_idempotent(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    kwargs = dict(candidate_csv=candidate_csv, decisions_csv=decisions_csv, merged_csv=merged_csv)

    review.save_decision("rev4_strict", review_status=review.NEEDS_MORE_CONTEXT, reviewer="A", **kwargs)
    review.save_decision("rev4_strict", review_status=review.NEEDS_MORE_CONTEXT, reviewer="A", **kwargs)

    rows = read_csv(decisions_csv)
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "rev4_strict"
    assert rows[0]["review_status"] == review.NEEDS_MORE_CONTEXT


def test_merged_export_preserves_original_parser_fields_and_corrections_are_separate(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    review.save_decision(
        "rev4_strict",
        review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
        reviewer="A",
        corrected_fields={"corrected_child_1_symbol": "XYZ"},
        candidate_csv=candidate_csv,
        decisions_csv=decisions_csv,
        merged_csv=merged_csv,
    )

    merged = {row["candidate_id"]: row for row in read_csv(merged_csv)}
    assert merged["rev4_strict"]["child_1_symbol"] == ""
    assert merged["rev4_strict"]["corrected_child_1_symbol"] == "XYZ"
    assert json.loads(merged["rev4_strict"]["changed_fields_json"])["child_1_symbol"]["corrected"] == "XYZ"


def test_training_eligibility_validation_blocks_unsafe_statuses(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    kwargs = dict(candidate_csv=candidate_csv, decisions_csv=decisions_csv, merged_csv=merged_csv)

    review.save_decision(
        "rev4_strict",
        review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
        use_for_structure_training=True,
        reviewer="A",
        **kwargs,
    )

    for status in [review.FALSE_POSITIVE, review.NEEDS_MORE_CONTEXT, review.REJECTED_GIBBERISH]:
        with pytest.raises(review.Phase4ReviewError):
            review.save_decision(
                "rev4_false",
                review_status=status,
                use_for_structure_training=True,
                reviewer="A",
                **kwargs,
            )


def test_reviewer_required_and_reviewed_at_recorded(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths

    with pytest.raises(review.Phase4ReviewError):
        review.save_decision(
            "rev4_strict",
            review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
            reviewer="",
            candidate_csv=candidate_csv,
            decisions_csv=decisions_csv,
            merged_csv=merged_csv,
        )

    saved = review.save_decision(
        "rev4_strict",
        review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
        reviewer="Analyst",
        candidate_csv=candidate_csv,
        decisions_csv=decisions_csv,
        merged_csv=merged_csv,
    )
    assert saved["reviewed_at"].endswith("Z")


def test_clearing_decision_returns_to_pending(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    kwargs = dict(candidate_csv=candidate_csv, decisions_csv=decisions_csv, merged_csv=merged_csv)

    review.save_decision("rev4_strict", review_status=review.FALSE_POSITIVE, reviewer="A", **kwargs)
    pending = review.clear_decision("rev4_strict", **kwargs)

    assert pending["review_status"] == review.PENDING_REVIEW
    assert read_csv(decisions_csv) == []
    merged = {row["candidate_id"]: row for row in read_csv(merged_csv)}
    assert merged["rev4_strict"]["review_status"] == review.PENDING_REVIEW


def test_byte_safe_handling_of_commas_quotes_ampersands_and_line_breaks(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    notes = 'quoted, comma & ampersand\nsecond line "ok"'

    review.save_decision(
        "rev4_strict",
        review_status=review.DO_NOT_USE_FOR_TRAINING,
        reviewer="A",
        review_notes=notes,
        corrected_fields={"corrected_clause_1_text": 'FULL, "PROFIT" & BOOK\nABC'},
        candidate_csv=candidate_csv,
        decisions_csv=decisions_csv,
        merged_csv=merged_csv,
    )

    row = read_csv(decisions_csv)[0]
    assert row["review_notes"] == notes
    assert row["corrected_clause_1_text"] == 'FULL, "PROFIT" & BOOK\nABC'


def test_streamlit_rerun_style_repeated_save_does_not_duplicate(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    kwargs = dict(candidate_csv=candidate_csv, decisions_csv=decisions_csv, merged_csv=merged_csv)
    for _ in range(5):
        review.save_decision("rev4_false", review_status=review.FALSE_POSITIVE, reviewer="A", **kwargs)

    rows = read_csv(decisions_csv)
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "rev4_false"


def test_filters_and_default_ordering(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    review.save_decision(
        "rev4_false",
        review_status=review.FALSE_POSITIVE,
        reviewer="A",
        candidate_csv=candidate_csv,
        decisions_csv=decisions_csv,
        merged_csv=merged_csv,
    )
    rows = review.attach_decisions(review.load_candidates(candidate_csv), review.load_decisions(decisions_csv))

    ordered = review.filter_candidates(rows)
    assert [row["candidate_id"] for row in ordered] == ["rev4_strict", "rev4_buy", "rev4_prose", "rev4_false"]
    assert review.filter_candidates(rows, review_status=review.FALSE_POSITIVE)[0]["candidate_id"] == "rev4_false"
    assert review.filter_candidates(rows, candidate_scope="STRICT")[0]["candidate_id"] == "rev4_strict"
    assert {row["candidate_id"] for row in review.filter_candidates(rows, candidate_scope="BROAD")} == {
        "rev4_prose",
        "rev4_false",
    }


def test_progress_summary_counts_and_reports_exclude_full_text(review_paths):
    candidate_csv, decisions_csv, merged_csv, reports_dir = review_paths
    kwargs = dict(candidate_csv=candidate_csv, decisions_csv=decisions_csv, merged_csv=merged_csv)
    review.save_decision(
        "rev4_strict",
        review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
        use_for_structure_training=True,
        reviewer="A",
        **kwargs,
    )
    review.save_decision("rev4_false", review_status=review.FALSE_POSITIVE, reviewer="A", **kwargs)

    summary = review.write_review_reports(
        review.ReviewPaths(
            candidate_csv=candidate_csv,
            decisions_csv=decisions_csv,
            merged_csv=merged_csv,
            reports_dir=reports_dir,
        )
    )

    assert summary["total_candidates"] == 4
    assert summary["reviewed_candidates"] == 2
    assert summary["pending_candidates"] == 2
    assert summary["confirmed_ordered_close_then_entry"] == 1
    assert summary["false_positives"] == 1
    assert summary["structure_training_eligible_records"] == 1
    for report_path in reports_dir.iterdir():
        assert "FULL PROFIT BOOK IN ABC" not in report_path.read_text(encoding="utf-8")


def test_full_close_plus_sell_or_buy_confirms_close_then_entry_without_prior_side(review_paths):
    candidate_csv, _decisions_csv, _merged_csv, _reports_dir = review_paths
    rows = {row["candidate_id"]: row for row in review.load_candidates(candidate_csv)}

    assert review.ordered_close_then_entry_status(rows["rev4_strict"]) == review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY
    assert review.ordered_close_then_entry_status(rows["rev4_buy"]) == review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY


def test_portfolio_context_resolves_outcome_only_when_prior_side_is_known():
    assert review.resolve_portfolio_effect("LONG", "SELL") == review.REVERSAL
    assert review.resolve_portfolio_effect("SHORT", "BUY") == review.REVERSAL
    assert review.resolve_portfolio_effect("LONG", "BUY") == review.SAME_SIDE_REENTRY
    assert review.resolve_portfolio_effect("SHORT", "SELL") == review.SAME_SIDE_REENTRY
    assert review.resolve_portfolio_effect(None, "SELL") == review.NOT_EVALUATED


def test_missing_database_context_is_not_text_level_needs_context(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    saved = review.save_decision(
        "rev4_strict",
        review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
        portfolio_effect_status=review.NOT_EVALUATED,
        reviewer="A",
        candidate_csv=candidate_csv,
        decisions_csv=decisions_csv,
        merged_csv=merged_csv,
    )

    assert saved["review_status"] == review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY
    assert saved["portfolio_effect_status"] == review.NOT_EVALUATED
    assert saved["review_bundle_type"] == review.ORDERED_CLOSE_THEN_ENTRY


def test_existing_decision_can_be_updated_without_duplicate_rows(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    kwargs = dict(candidate_csv=candidate_csv, decisions_csv=decisions_csv, merged_csv=merged_csv)

    first = review.save_decision("rev4_strict", review_status=review.NEEDS_MORE_CONTEXT, reviewer="A", **kwargs)
    second = review.save_decision(
        "rev4_strict",
        review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
        use_for_structure_training=True,
        reviewer="B",
        review_notes="updated",
        **kwargs,
    )

    rows = read_csv(decisions_csv)
    assert len(rows) == 1
    assert first["decision_revision"] == "1"
    assert second["decision_revision"] == "2"
    assert rows[0]["previous_review_status"] == review.NEEDS_MORE_CONTEXT
    assert rows[0]["review_status"] == review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY
    assert rows[0]["reviewer"] == "B"
    assert rows[0]["review_notes"] == "updated"


def test_legacy_decisions_remain_readable(tmp_path):
    decisions_csv = tmp_path / "decisions.csv"
    decisions_csv.parent.mkdir(parents=True, exist_ok=True)
    with decisions_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["candidate_id", "review_status", "use_for_training", "reviewer", "reviewed_at"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerow(
            {
                "candidate_id": "rev4_legacy",
                "review_status": review.CONFIRMED_ORDERED_REVERSAL,
                "use_for_training": "true",
                "reviewer": "A",
                "reviewed_at": "2026-01-01T00:00:00Z",
            }
        )

    loaded = review.load_decisions(decisions_csv)[0]
    assert loaded["review_status"] == review.CONFIRMED_ORDERED_REVERSAL
    assert loaded["use_for_structure_training"] == "true"
    assert loaded["use_for_portfolio_effect_training"] == "false"
    assert loaded["portfolio_effect_status"] == review.NOT_EVALUATED


def test_structure_and_portfolio_training_flags_are_independently_validated(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    kwargs = dict(candidate_csv=candidate_csv, decisions_csv=decisions_csv, merged_csv=merged_csv)

    review.save_decision(
        "rev4_strict",
        review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
        use_for_structure_training=True,
        use_for_portfolio_effect_training=False,
        portfolio_effect_status=review.NOT_EVALUATED,
        reviewer="A",
        **kwargs,
    )
    review.save_decision(
        "rev4_buy",
        review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
        use_for_structure_training=False,
        use_for_portfolio_effect_training=True,
        portfolio_effect_status=review.SAME_SIDE_REENTRY,
        reviewer="A",
        **kwargs,
    )
    with pytest.raises(review.Phase4ReviewError):
        review.save_decision(
            "rev4_prose",
            review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
            use_for_portfolio_effect_training=True,
            portfolio_effect_status=review.NOT_EVALUATED,
            reviewer="A",
            **kwargs,
        )


def test_not_evaluated_records_are_excluded_from_portfolio_metrics(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths
    kwargs = dict(candidate_csv=candidate_csv, decisions_csv=decisions_csv, merged_csv=merged_csv)
    review.save_decision(
        "rev4_strict",
        review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
        portfolio_effect_status=review.NOT_EVALUATED,
        reviewer="A",
        **kwargs,
    )
    review.save_decision(
        "rev4_buy",
        review_status=review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
        portfolio_effect_status=review.REVERSAL,
        reviewer="A",
        **kwargs,
    )

    rows = review.merge_candidates_and_decisions(candidate_csv=candidate_csv, decisions_csv=decisions_csv)
    denominator = review.portfolio_metrics_denominator(rows)
    assert [row["candidate_id"] for row in denominator] == ["rev4_buy"]


def test_candidate_ids_not_found_are_rejected(review_paths):
    candidate_csv, decisions_csv, merged_csv, _reports_dir = review_paths

    with pytest.raises(review.Phase4ReviewError):
        review.save_decision(
            "rev4_missing",
            review_status=review.FALSE_POSITIVE,
            reviewer="A",
            candidate_csv=candidate_csv,
            decisions_csv=decisions_csv,
            merged_csv=merged_csv,
        )
