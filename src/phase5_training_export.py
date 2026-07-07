from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from src.config import PROJECT_ROOT, REPORTS_DIR
from src.hybrid_parser import HybridParser
from src.phase4_review import (
    CONFIRMED_MULTI_CLAUSE_REVIEW_ONLY,
    CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
    CONFIRMED_ORDERED_REVERSAL,
    DECISIONS_CSV,
    DO_NOT_USE_FOR_TRAINING,
    FALSE_POSITIVE,
    MERGED_REVIEW_CSV,
    NEEDS_MORE_CONTEXT,
    NOT_EVALUATED,
    PENDING_REVIEW,
    REJECTED_GIBBERISH,
    REVERSAL,
    SAME_SIDE_REENTRY,
)


CANDIDATE_CSV = PROJECT_ROOT / "data" / "review" / "phase4_reversal_candidates.csv"
REVIEWED_CSV = MERGED_REVIEW_CSV
DERIVED_DIR = PROJECT_ROOT / "data" / "derived"

LABEL_CSV = DERIVED_DIR / "phase5_ordered_close_then_entry_labels.csv"
LABEL_JSONL = DERIVED_DIR / "phase5_ordered_close_then_entry_labels.jsonl"

AUDIT_JSON = REPORTS_DIR / "phase5_review_audit.json"
EXPORT_SUMMARY_JSON = REPORTS_DIR / "phase5_training_export_summary.json"
BASELINE_EVALUATION_JSON = REPORTS_DIR / "phase5_parser_baseline_evaluation.json"
BASELINE_FAILURES_CSV = REPORTS_DIR / "phase5_parser_baseline_failures.csv"

EXPECTED_AUDIT = {
    "total_candidates": 153,
    "reviewed_count": 153,
    "pending_count": 0,
    "status_counts": {
        CONFIRMED_ORDERED_CLOSE_THEN_ENTRY: 13,
        CONFIRMED_MULTI_CLAUSE_REVIEW_ONLY: 86,
        FALSE_POSITIVE: 50,
        NEEDS_MORE_CONTEXT: 3,
        DO_NOT_USE_FOR_TRAINING: 1,
        REJECTED_GIBBERISH: 0,
    },
    "structure_training_eligible_count": 13,
    "portfolio_effect_training_eligible_count": 0,
    "deprecated_confirmed_ordered_reversal_count": 0,
    "inconsistent_flags_count": 0,
}

EXPORT_FIELDS = [
    "candidate_id",
    "source_message_id",
    "source_row_number",
    "raw_text",
    "normalized_text",
    "human_review_status",
    "label_type",
    "clause_1_text",
    "clause_2_text",
    "child_1_surface_instruction",
    "child_1_symbol",
    "child_1_contract_month",
    "child_1_market_group",
    "child_1_position_effect",
    "child_2_surface_instruction",
    "child_2_symbol",
    "child_2_contract_month",
    "child_2_market_group",
    "child_2_position_effect",
    "child_2_requested_side",
    "child_2_entry_capacity_pct",
    "child_2_stop_loss",
    "child_2_targets",
    "same_non_direction_identity",
    "portfolio_effect_status",
    "use_for_structure_training",
    "use_for_portfolio_effect_training",
    "reviewer",
    "reviewed_at",
    "changed_fields_json",
]

EVALUATION_FIELDS = [
    "ordered_close_then_entry_detection",
    "clause_split_accuracy",
    "close_clause_surface_instruction",
    "close_clause_symbol",
    "close_clause_contract_month",
    "close_clause_market_group",
    "close_clause_position_effect",
    "entry_clause_surface_instruction",
    "entry_clause_symbol",
    "entry_clause_contract_month",
    "entry_clause_market_group",
    "entry_clause_requested_side",
    "entry_clause_capacity_percentage",
    "entry_clause_stop_loss",
    "entry_clause_targets",
    "same_non_direction_identity",
]

DISALLOWED_EXPORT_STATUSES = {
    CONFIRMED_MULTI_CLAUSE_REVIEW_ONLY,
    FALSE_POSITIVE,
    NEEDS_MORE_CONTEXT,
    DO_NOT_USE_FOR_TRAINING,
    REJECTED_GIBBERISH,
}


class Phase5ExportError(RuntimeError):
    pass


@dataclass(frozen=True)
class Phase5Paths:
    candidate_csv: Path = CANDIDATE_CSV
    decisions_csv: Path = DECISIONS_CSV
    reviewed_csv: Path = REVIEWED_CSV
    label_csv: Path = LABEL_CSV
    label_jsonl: Path = LABEL_JSONL
    reports_dir: Path = REPORTS_DIR


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in fieldnames} for row in rows])


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            f.write("\n")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _json_loads(value: str, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _clean_decimal(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except Exception:
        return str(value)


def _normalize_targets(value: Any) -> str:
    if value in (None, ""):
        return "[]"
    if isinstance(value, str):
        parsed = _json_loads(value, None)
        if parsed is None:
            parsed = [part.strip() for part in value.split("|") if part.strip()]
    else:
        parsed = value
    if not isinstance(parsed, list):
        parsed = [parsed]
    return json.dumps([_clean_decimal(item) for item in parsed], separators=(",", ":"))


def _effective(row: dict[str, str], corrected_field: str, source_field: str) -> str:
    corrected_value = row.get(corrected_field, "")
    return corrected_value if corrected_value not in (None, "") else row.get(source_field, "")


def _child(row: dict[str, str], index: int) -> dict[str, Any]:
    return _json_loads(row.get(f"child_{index}_json", ""), {})


def _child_field(row: dict[str, str], index: int, field: str) -> Any:
    return _child(row, index).get(field, "")


def _side_from_instruction(instruction: str, fallback: Any = "") -> str:
    if fallback not in (None, ""):
        return str(fallback)
    instruction = (instruction or "").upper()
    if instruction == "BUY":
        return "LONG"
    if instruction == "SELL":
        return "SHORT"
    return ""


def _fallback_clauses(row: dict[str, str]) -> tuple[str, str]:
    raw_text = row.get("raw_text", "")
    clause_1 = row.get("clause_1_text", "")
    clause_2 = row.get("clause_2_text", "")
    if clause_1 and clause_2:
        return clause_1, clause_2

    if "&" in raw_text:
        left, right = raw_text.split("&", 1)
        return left.strip(), right.strip()

    match = re.search(r"\b(BUY|SELL)\b", raw_text, re.IGNORECASE)
    if match:
        left = raw_text[: match.start()].strip(" ;,&")
        left = re.sub(r"\s+\bAND\b\s*$", "", left, flags=re.IGNORECASE).strip(" ;,&")
        right = raw_text[match.start():].strip(" ;,&")
        return clause_1 if clause_1 and clause_1 != raw_text else left, clause_2 or right

    return clause_1, clause_2


def _fallback_instruction(text: str) -> str:
    match = re.search(r"\b(BUY|SELL)\b", text or "", re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _explicit_percentage(text: str) -> str:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text or "")
    return _clean_decimal(match.group(1)) if match else ""


def _same_identity_from_labels(row: dict[str, Any]) -> str:
    return "true" if all(
        str(row.get(f"child_1_{field}", "")) == str(row.get(f"child_2_{field}", ""))
        for field in ("symbol", "contract_month", "market_group")
    ) else "false"


def _status_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts = Counter(row.get("review_status", "") for row in rows)
    for status in EXPECTED_AUDIT["status_counts"]:
        counts.setdefault(status, 0)
    return dict(sorted(counts.items()))


def audit_review_decisions(paths: Phase5Paths = Phase5Paths()) -> dict[str, Any]:
    candidates = _read_csv(paths.candidate_csv)
    decisions = _read_csv(paths.decisions_csv)
    reviewed = _read_csv(paths.reviewed_csv)

    candidate_ids = [row.get("candidate_id", "") for row in candidates]
    candidate_id_set = set(candidate_ids)
    decision_ids = [row.get("candidate_id", "") for row in decisions]
    duplicate_decision_ids = sorted(
        candidate_id for candidate_id, count in Counter(decision_ids).items() if candidate_id and count > 1
    )

    status_counts = _status_counts(reviewed)
    reviewed_rows = [row for row in reviewed if row.get("review_status") != PENDING_REVIEW]
    legacy_true = [row for row in reviewed if _bool(row.get("use_for_training"))]
    structure_true = [row for row in reviewed if _bool(row.get("use_for_structure_training"))]
    portfolio_true = [row for row in reviewed if _bool(row.get("use_for_portfolio_effect_training"))]

    failures: dict[str, Any] = {
        "pending_candidate_ids": [
            row.get("candidate_id", "") for row in reviewed if row.get("review_status") == PENDING_REVIEW
        ],
        "missing_reviewer_ids": [
            row.get("candidate_id", "") for row in reviewed_rows if not row.get("reviewer", "").strip()
        ],
        "missing_reviewed_at_ids": [
            row.get("candidate_id", "") for row in reviewed_rows if not row.get("reviewed_at", "").strip()
        ],
        "decision_ids_missing_from_candidate_csv": sorted(set(decision_ids) - candidate_id_set),
        "duplicate_decision_ids": duplicate_decision_ids,
        "structure_flag_bad_status_ids": [
            row.get("candidate_id", "")
            for row in structure_true
            if row.get("review_status") != CONFIRMED_ORDERED_CLOSE_THEN_ENTRY
        ],
        "portfolio_flag_not_evaluated_ids": [
            row.get("candidate_id", "")
            for row in portfolio_true
            if row.get("portfolio_effect_status") == NOT_EVALUATED
        ],
        "portfolio_flag_bad_status_ids": [
            row.get("candidate_id", "")
            for row in portfolio_true
            if row.get("portfolio_effect_status") not in {REVERSAL, SAME_SIDE_REENTRY}
        ],
        "legacy_mapped_to_portfolio_effect_ids": [
            row.get("candidate_id", "")
            for row in legacy_true
            if _bool(row.get("use_for_portfolio_effect_training"))
        ],
        "deprecated_confirmed_ordered_reversal_ids": [
            row.get("candidate_id", "")
            for row in reviewed
            if row.get("review_status") == CONFIRMED_ORDERED_REVERSAL
        ],
        "eligible_rows_missing_required_fields": [],
    }

    for row in reviewed:
        if not _is_structure_eligible(row):
            continue
        label = build_export_row(row)
        required = [
            "candidate_id",
            "source_message_id",
            "source_row_number",
            "clause_1_text",
            "clause_2_text",
            "child_1_surface_instruction",
            "child_1_symbol",
            "child_1_position_effect",
            "child_2_surface_instruction",
            "child_2_symbol",
            "child_2_position_effect",
            "child_2_requested_side",
        ]
        missing = [field for field in required if not str(label.get(field, "")).strip()]
        if label.get("same_non_direction_identity") != "true":
            missing.append("same_non_direction_identity")
        if missing:
            failures["eligible_rows_missing_required_fields"].append(
                {"candidate_id": row.get("candidate_id", ""), "missing_fields": missing}
            )

    inconsistent_flags_count = sum(
        len(value) for value in failures.values() if isinstance(value, list)
    )
    audit = {
        "total_candidates": len(reviewed),
        "candidate_rows": len(candidates),
        "decision_rows": len(decisions),
        "unique_candidate_ids": len(candidate_id_set),
        "unique_decision_ids": len(set(decision_ids)),
        "reviewed_count": len(reviewed_rows),
        "pending_count": len(failures["pending_candidate_ids"]),
        "status_counts": status_counts,
        "structure_training_eligible_count": len(structure_true),
        "portfolio_effect_training_eligible_count": len(
            [
                row for row in portfolio_true
                if row.get("portfolio_effect_status") in {REVERSAL, SAME_SIDE_REENTRY}
            ]
        ),
        "deprecated_confirmed_ordered_reversal_count": len(failures["deprecated_confirmed_ordered_reversal_ids"]),
        "legacy_use_for_training_true_count": len(legacy_true),
        "mapped_to_structure_training_count": len(
            [row for row in legacy_true if _bool(row.get("use_for_structure_training"))]
        ),
        "mapped_to_portfolio_effect_training_count": len(failures["legacy_mapped_to_portfolio_effect_ids"]),
        "inconsistent_flags_count": inconsistent_flags_count,
        "failures": failures,
        "source_hashes": {
            "candidate_csv_sha256": _sha256(paths.candidate_csv),
            "decisions_csv_sha256": _sha256(paths.decisions_csv),
            "reviewed_csv_sha256": _sha256(paths.reviewed_csv),
        },
    }
    return audit


def assert_expected_audit(audit: dict[str, Any]) -> None:
    differences: dict[str, Any] = {}
    for key, expected in EXPECTED_AUDIT.items():
        actual = audit.get(key)
        if key == "status_counts":
            actual_subset = {status: actual.get(status, 0) for status in expected}
            if actual_subset != expected:
                differences[key] = {"expected": expected, "actual": actual_subset}
        elif actual != expected:
            differences[key] = {"expected": expected, "actual": actual}
    if audit["inconsistent_flags_count"] != 0:
        differences["audit_failures"] = audit["failures"]
    if differences:
        raise Phase5ExportError(f"Phase 5 audit does not match expected baseline: {json.dumps(differences, sort_keys=True)}")


def _is_structure_eligible(row: dict[str, str]) -> bool:
    return (
        row.get("review_status") == CONFIRMED_ORDERED_CLOSE_THEN_ENTRY
        and _bool(row.get("use_for_structure_training"))
        and not _bool(row.get("use_for_portfolio_effect_training"))
        and row.get("review_status") not in DISALLOWED_EXPORT_STATUSES
    )


def build_export_row(row: dict[str, str]) -> dict[str, Any]:
    child1 = _child(row, 1)
    child2 = _child(row, 2)
    clause_1_text, clause_2_text = _fallback_clauses(row)
    child_1_symbol = _effective(row, "corrected_child_1_symbol", "child_1_symbol") or child1.get("symbol", "")
    child_2_symbol = (
        _effective(row, "corrected_child_2_symbol", "child_2_symbol")
        or child2.get("symbol", "")
        or child_1_symbol
    )
    child_1_market = child1.get("market_group", "") or row.get("segment_name", "")
    child_2_market = child2.get("market_group", "") or child_1_market or row.get("segment_name", "")
    child_2_instruction = _effective(
        row,
        "corrected_child_2_surface_instruction",
        "child_2_surface_instruction",
    ) or child2.get("surface_instruction", "") or _fallback_instruction(clause_2_text)
    child_2_capacity = _effective(
        row,
        "corrected_child_2_entry_capacity_pct",
        "child_2_entry_capacity_pct",
    ) or child2.get("entry_capacity_pct", "") or _explicit_percentage(clause_2_text)
    child_2_stop_loss = child2.get("stop_loss", "") or child1.get("stop_loss", "")
    child_2_targets = child2.get("targets", []) or child1.get("targets", [])
    label = {
        "candidate_id": row.get("candidate_id", ""),
        "source_message_id": row.get("source_message_id", ""),
        "source_row_number": row.get("source_row_number", ""),
        "raw_text": row.get("raw_text", ""),
        "normalized_text": row.get("normalized_text", ""),
        "human_review_status": row.get("review_status", ""),
        "label_type": "ORDERED_CLOSE_THEN_ENTRY",
        "clause_1_text": row.get("corrected_clause_1_text", "") or clause_1_text,
        "clause_2_text": row.get("corrected_clause_2_text", "") or clause_2_text,
        "child_1_surface_instruction": _effective(
            row,
            "corrected_child_1_surface_instruction",
            "child_1_surface_instruction",
        ) or child1.get("surface_instruction", ""),
        "child_1_symbol": child_1_symbol,
        "child_1_contract_month": child1.get("contract_month", "") or "",
        "child_1_market_group": child_1_market,
        "child_1_position_effect": _effective(
            row,
            "corrected_child_1_position_effect",
            "child_1_position_effect",
        ) or child1.get("position_effect", ""),
        "child_2_surface_instruction": child_2_instruction,
        "child_2_symbol": child_2_symbol,
        "child_2_contract_month": child2.get("contract_month", "") or child1.get("contract_month", "") or "",
        "child_2_market_group": child_2_market,
        "child_2_position_effect": _effective(
            row,
            "corrected_child_2_position_effect",
            "child_2_position_effect",
        ) or child2.get("position_effect", "") or "OPEN",
        "child_2_requested_side": _side_from_instruction(
            child_2_instruction,
            child2.get("resolved_position_side", "") or child2.get("direction", ""),
        ),
        "child_2_entry_capacity_pct": _clean_decimal(child_2_capacity),
        "child_2_stop_loss": _clean_decimal(child_2_stop_loss),
        "child_2_targets": _normalize_targets(child_2_targets),
        "same_non_direction_identity": "true"
        if row.get("same_instrument_identity", "").lower() == "true"
        else "",
        "portfolio_effect_status": row.get("portfolio_effect_status", ""),
        "use_for_structure_training": "true" if _bool(row.get("use_for_structure_training")) else "false",
        "use_for_portfolio_effect_training": "true"
        if _bool(row.get("use_for_portfolio_effect_training"))
        else "false",
        "reviewer": row.get("reviewer", ""),
        "reviewed_at": row.get("reviewed_at", ""),
        "changed_fields_json": row.get("changed_fields_json", "") or "{}",
    }
    if not label["same_non_direction_identity"]:
        label["same_non_direction_identity"] = _same_identity_from_labels(label)
    return label


def export_labels(paths: Phase5Paths = Phase5Paths(), *, enforce_expected: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    audit = audit_review_decisions(paths)
    if enforce_expected:
        assert_expected_audit(audit)
    reviewed = _read_csv(paths.reviewed_csv)
    labels = [build_export_row(row) for row in reviewed if _is_structure_eligible(row)]
    labels.sort(key=lambda row: (int(row["source_row_number"] or 0), row["candidate_id"]))
    if enforce_expected and len(labels) != EXPECTED_AUDIT["structure_training_eligible_count"]:
        raise Phase5ExportError(f"Expected 13 exported labels, got {len(labels)}")

    _write_csv(paths.label_csv, labels, EXPORT_FIELDS)
    _write_jsonl(paths.label_jsonl, labels)

    summary = {
        "export_count": len(labels),
        "label_type": "ORDERED_CLOSE_THEN_ENTRY",
        "structure_training_exported": True,
        "portfolio_effect_exported": False,
        "portfolio_effect_skip_reason": "No rows are eligible for portfolio-effect training.",
        "exported_candidate_ids": [row["candidate_id"] for row in labels],
        "excluded_status_counts": {
            status: audit["status_counts"].get(status, 0)
            for status in DISALLOWED_EXPORT_STATUSES
        },
        "legacy_use_for_training_true_count": audit["legacy_use_for_training_true_count"],
        "mapped_to_structure_training_count": audit["mapped_to_structure_training_count"],
        "mapped_to_portfolio_effect_training_count": audit["mapped_to_portfolio_effect_training_count"],
        "output_files": {
            "label_csv": str(paths.label_csv),
            "label_jsonl": str(paths.label_jsonl),
        },
        "source_hashes": audit["source_hashes"],
    }
    _write_json(paths.reports_dir / AUDIT_JSON.name, audit)
    _write_json(paths.reports_dir / EXPORT_SUMMARY_JSON.name, summary)
    return labels, summary


def _canonical(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return _normalize_targets(value)
    return str(value or "").strip()


def _eval_match(expected: Any, actual: Any, *, decimal: bool = False, targets: bool = False) -> bool:
    if targets:
        return _normalize_targets(expected) == _normalize_targets(actual)
    if decimal:
        return _clean_decimal(expected) == _clean_decimal(actual)
    return _canonical(expected).upper() == _canonical(actual).upper()


def _parser_prediction(label: dict[str, Any], parser: HybridParser) -> dict[str, Any]:
    bundle = parser.parse_bundle(
        label["raw_text"],
        source_message_id=label["source_message_id"],
        existing_long=False,
        existing_short=False,
        has_holdings_context=False,
        market_group=label.get("child_1_market_group") or None,
    )
    child1 = bundle.child_events[0] if len(bundle.child_events) >= 1 else None
    child2 = bundle.child_events[1] if len(bundle.child_events) >= 2 else None
    return {
        "ordered_close_then_entry_detection": (
            bundle.is_ordered
            and len(bundle.child_events) == 2
            and child1 is not None
            and child2 is not None
            and child1.position_effect == "CLOSE"
            and child2.surface_instruction in {"BUY", "SELL"}
        ),
        "clause_1_text": child1.clause_text if child1 else "",
        "clause_2_text": child2.clause_text if child2 else "",
        "child_1_surface_instruction": child1.surface_instruction if child1 else "",
        "child_1_symbol": child1.symbol if child1 else "",
        "child_1_contract_month": child1.contract_month if child1 else "",
        "child_1_market_group": child1.market_group if child1 else "",
        "child_1_position_effect": child1.position_effect if child1 else "",
        "child_2_surface_instruction": child2.surface_instruction if child2 else "",
        "child_2_symbol": child2.symbol if child2 else "",
        "child_2_contract_month": child2.contract_month if child2 else "",
        "child_2_market_group": child2.market_group if child2 else "",
        "child_2_requested_side": _side_from_instruction(
            child2.surface_instruction if child2 else "",
            (child2.resolved_position_side if child2 else "") or (child2.direction if child2 else ""),
        ),
        "child_2_entry_capacity_pct": child2.entry_capacity_pct if child2 else "",
        "child_2_stop_loss": child2.stop_loss if child2 else "",
        "child_2_targets": child2.targets if child2 else [],
        "same_non_direction_identity": "true"
        if child1 and child2 and _parser_same_identity(child1, child2)
        else "false",
    }


def _parser_same_identity(child1: Any, child2: Any) -> bool:
    return (
        (child1.symbol or "") == (child2.symbol or "")
        and (child1.contract_month or "") == (child2.contract_month or "")
        and (child1.market_group or "") == (child2.market_group or "")
    )


def evaluate_parser_baseline(
    labels: list[dict[str, Any]],
    paths: Phase5Paths = Phase5Paths(),
    parser: Optional[HybridParser] = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    parser = parser or HybridParser()
    counts = {field: {"passed": 0, "failed": 0, "total": len(labels)} for field in EVALUATION_FIELDS}
    failures: list[dict[str, str]] = []

    for label in labels:
        predicted = _parser_prediction(label, parser)
        checks = {
            "ordered_close_then_entry_detection": bool(predicted["ordered_close_then_entry_detection"]),
            "clause_split_accuracy": (
                _eval_match(label["clause_1_text"], predicted["clause_1_text"])
                and _eval_match(label["clause_2_text"], predicted["clause_2_text"])
            ),
            "close_clause_surface_instruction": _eval_match(
                label["child_1_surface_instruction"], predicted["child_1_surface_instruction"]
            ),
            "close_clause_symbol": _eval_match(label["child_1_symbol"], predicted["child_1_symbol"]),
            "close_clause_contract_month": _eval_match(
                label["child_1_contract_month"], predicted["child_1_contract_month"]
            ),
            "close_clause_market_group": _eval_match(
                label["child_1_market_group"], predicted["child_1_market_group"]
            ),
            "close_clause_position_effect": _eval_match(
                label["child_1_position_effect"], predicted["child_1_position_effect"]
            ),
            "entry_clause_surface_instruction": _eval_match(
                label["child_2_surface_instruction"], predicted["child_2_surface_instruction"]
            ),
            "entry_clause_symbol": _eval_match(label["child_2_symbol"], predicted["child_2_symbol"]),
            "entry_clause_contract_month": _eval_match(
                label["child_2_contract_month"], predicted["child_2_contract_month"]
            ),
            "entry_clause_market_group": _eval_match(
                label["child_2_market_group"], predicted["child_2_market_group"]
            ),
            "entry_clause_requested_side": _eval_match(
                label["child_2_requested_side"], predicted["child_2_requested_side"]
            ),
            "entry_clause_capacity_percentage": _eval_match(
                label["child_2_entry_capacity_pct"], predicted["child_2_entry_capacity_pct"], decimal=True
            ),
            "entry_clause_stop_loss": _eval_match(
                label["child_2_stop_loss"], predicted["child_2_stop_loss"], decimal=True
            ),
            "entry_clause_targets": _eval_match(
                label["child_2_targets"], predicted["child_2_targets"], targets=True
            ),
            "same_non_direction_identity": _eval_match(
                label["same_non_direction_identity"], predicted["same_non_direction_identity"]
            ),
        }
        for field, passed in checks.items():
            counts[field]["passed" if passed else "failed"] += 1
            if not passed:
                failures.append(
                    {
                        "candidate_id": label["candidate_id"],
                        "field_name": field,
                        "expected": _safe_failure_value(_expected_value_for_failure(field, label)),
                        "actual": _safe_failure_value(_actual_value_for_failure(field, predicted)),
                    }
                )

    metrics = {
        field: {
            **value,
            "accuracy": round(value["passed"] / value["total"], 6) if value["total"] else 0.0,
        }
        for field, value in counts.items()
    }
    summary = {
        "label_count": len(labels),
        "field_metrics": metrics,
        "failure_count": len(failures),
        "failed_candidate_ids": sorted({row["candidate_id"] for row in failures}),
        "notes": [
            "Parser baseline only; no model retraining or parser patching was performed.",
            "Failure rows contain candidate IDs and field names only, not raw source text.",
        ],
    }
    _write_json(paths.reports_dir / BASELINE_EVALUATION_JSON.name, summary)
    _write_csv(
        paths.reports_dir / BASELINE_FAILURES_CSV.name,
        failures,
        ["candidate_id", "field_name", "expected", "actual"],
    )
    return summary, failures


def _expected_value_for_failure(field: str, label: dict[str, Any]) -> Any:
    mapping = {
        "ordered_close_then_entry_detection": "true",
        "clause_split_accuracy": "clause_1_text|clause_2_text",
        "close_clause_surface_instruction": "child_1_surface_instruction",
        "close_clause_symbol": "child_1_symbol",
        "close_clause_contract_month": "child_1_contract_month",
        "close_clause_market_group": "child_1_market_group",
        "close_clause_position_effect": "child_1_position_effect",
        "entry_clause_surface_instruction": "child_2_surface_instruction",
        "entry_clause_symbol": "child_2_symbol",
        "entry_clause_contract_month": "child_2_contract_month",
        "entry_clause_market_group": "child_2_market_group",
        "entry_clause_requested_side": "child_2_requested_side",
        "entry_clause_capacity_percentage": "child_2_entry_capacity_pct",
        "entry_clause_stop_loss": "child_2_stop_loss",
        "entry_clause_targets": "child_2_targets",
        "same_non_direction_identity": "same_non_direction_identity",
    }
    key = mapping[field]
    if key == "true":
        return "true"
    if key == "clause_1_text|clause_2_text":
        return "<clause labels>"
    return label.get(key, "")


def _actual_value_for_failure(field: str, predicted: dict[str, Any]) -> Any:
    mapping = {
        "ordered_close_then_entry_detection": "ordered_close_then_entry_detection",
        "clause_split_accuracy": "clause_1_text|clause_2_text",
        "close_clause_surface_instruction": "child_1_surface_instruction",
        "close_clause_symbol": "child_1_symbol",
        "close_clause_contract_month": "child_1_contract_month",
        "close_clause_market_group": "child_1_market_group",
        "close_clause_position_effect": "child_1_position_effect",
        "entry_clause_surface_instruction": "child_2_surface_instruction",
        "entry_clause_symbol": "child_2_symbol",
        "entry_clause_contract_month": "child_2_contract_month",
        "entry_clause_market_group": "child_2_market_group",
        "entry_clause_requested_side": "child_2_requested_side",
        "entry_clause_capacity_percentage": "child_2_entry_capacity_pct",
        "entry_clause_stop_loss": "child_2_stop_loss",
        "entry_clause_targets": "child_2_targets",
        "same_non_direction_identity": "same_non_direction_identity",
    }
    key = mapping[field]
    if key == "clause_1_text|clause_2_text":
        return "<parser clauses>"
    return predicted.get(key, "")


def _safe_failure_value(value: Any) -> str:
    text = _normalize_targets(value) if isinstance(value, list) else str(value)
    return text if len(text) <= 80 else text[:77] + "..."


def run_phase5(paths: Phase5Paths = Phase5Paths(), *, enforce_expected: bool = True) -> dict[str, Any]:
    labels, export_summary = export_labels(paths, enforce_expected=enforce_expected)
    evaluation, failures = evaluate_parser_baseline(labels, paths)
    return {
        "audit_path": str(paths.reports_dir / AUDIT_JSON.name),
        "export_summary_path": str(paths.reports_dir / EXPORT_SUMMARY_JSON.name),
        "evaluation_path": str(paths.reports_dir / BASELINE_EVALUATION_JSON.name),
        "failure_path": str(paths.reports_dir / BASELINE_FAILURES_CSV.name),
        "label_csv": str(paths.label_csv),
        "label_jsonl": str(paths.label_jsonl),
        "export_count": export_summary["export_count"],
        "parser_failure_count": len(failures),
        "parser_failed_candidate_ids": evaluation["failed_candidate_ids"],
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export Phase 5 reviewed close-then-entry labels and baseline parser evaluation.")
    parser.add_argument("--candidate-csv", type=Path, default=CANDIDATE_CSV)
    parser.add_argument("--decisions-csv", type=Path, default=DECISIONS_CSV)
    parser.add_argument("--reviewed-csv", type=Path, default=REVIEWED_CSV)
    parser.add_argument("--label-csv", type=Path, default=LABEL_CSV)
    parser.add_argument("--label-jsonl", type=Path, default=LABEL_JSONL)
    parser.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    parser.add_argument("--no-expected-baseline", action="store_true")
    args = parser.parse_args(argv)

    paths = Phase5Paths(
        candidate_csv=args.candidate_csv,
        decisions_csv=args.decisions_csv,
        reviewed_csv=args.reviewed_csv,
        label_csv=args.label_csv,
        label_jsonl=args.label_jsonl,
        reports_dir=args.reports_dir,
    )
    result = run_phase5(paths, enforce_expected=not args.no_expected_baseline)
    print("Phase 5 reviewed training export and parser baseline complete.")
    print(f"export_count={result['export_count']}")
    print(f"parser_failure_count={result['parser_failure_count']}")
    print(f"label_csv={result['label_csv']}")
    print(f"label_jsonl={result['label_jsonl']}")
    print(f"reports_dir={args.reports_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
