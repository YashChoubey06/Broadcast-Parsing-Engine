from __future__ import annotations

import csv
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from scripts.phase4_reversal_candidate_scanner import CANDIDATE_FIELDS
from src.config import PROJECT_ROOT, REPORTS_DIR


CANDIDATE_CSV = PROJECT_ROOT / "data" / "review" / "phase4_reversal_candidates.csv"
DECISIONS_CSV = PROJECT_ROOT / "data" / "review" / "phase4_reversal_review_decisions.csv"
MERGED_REVIEW_CSV = PROJECT_ROOT / "data" / "review" / "phase4_reversal_candidates_reviewed.csv"

SUMMARY_JSON = REPORTS_DIR / "phase4_human_review_summary.json"
STATUS_COUNTS_CSV = REPORTS_DIR / "phase4_human_review_status_counts.csv"
TRAINING_ELIGIBILITY_CSV = REPORTS_DIR / "phase4_human_review_training_eligibility.csv"
PROGRESS_CSV = REPORTS_DIR / "phase4_human_review_progress.csv"

PENDING_REVIEW = "PENDING_REVIEW"
CONFIRMED_ORDERED_REVERSAL = "CONFIRMED_ORDERED_REVERSAL"
CONFIRMED_MULTI_CLAUSE_REVIEW_ONLY = "CONFIRMED_MULTI_CLAUSE_REVIEW_ONLY"
FALSE_POSITIVE = "FALSE_POSITIVE"
NEEDS_MORE_CONTEXT = "NEEDS_MORE_CONTEXT"
DO_NOT_USE_FOR_TRAINING = "DO_NOT_USE_FOR_TRAINING"
REJECTED_GIBBERISH = "REJECTED_GIBBERISH"

REVIEW_STATUSES = [
    PENDING_REVIEW,
    CONFIRMED_ORDERED_REVERSAL,
    CONFIRMED_MULTI_CLAUSE_REVIEW_ONLY,
    FALSE_POSITIVE,
    NEEDS_MORE_CONTEXT,
    DO_NOT_USE_FOR_TRAINING,
    REJECTED_GIBBERISH,
]

TRAINING_COMPATIBLE_STATUSES = {CONFIRMED_ORDERED_REVERSAL}

DECISION_FIELDS = [
    "candidate_id",
    "review_status",
    "use_for_training",
    "reviewer",
    "review_notes",
    "reviewed_at",
    "corrected_candidate_kind",
    "corrected_clause_1_text",
    "corrected_clause_2_text",
    "corrected_child_1_surface_instruction",
    "corrected_child_2_surface_instruction",
    "corrected_child_1_symbol",
    "corrected_child_2_symbol",
    "corrected_child_1_position_effect",
    "corrected_child_2_position_effect",
    "corrected_child_1_entry_capacity_pct",
    "corrected_child_2_entry_capacity_pct",
    "changed_fields_json",
]

CORRECTED_FIELDS = [
    field for field in DECISION_FIELDS if field.startswith("corrected_")
]

DEFAULT_DECISION = {
    field: "" for field in DECISION_FIELDS
}
DEFAULT_DECISION.update(
    {
        "review_status": PENDING_REVIEW,
        "use_for_training": "false",
    }
)

REVIEW_GUIDANCE = {
    CONFIRMED_ORDERED_REVERSAL: [
        "clause 1 fully closes the existing position",
        "clause 2 opens the opposite position",
        "both clauses concern the same instrument identity",
        "clause ordering is clear",
    ],
    CONFIRMED_MULTI_CLAUSE_REVIEW_ONLY: [
        "message contains valid multiple trade instructions",
        "but it is not a supported close-then-opposite-entry reversal",
    ],
    FALSE_POSITIVE: [
        "separator does not separate actionable trade instructions",
    ],
    NEEDS_MORE_CONTEXT: [
        "message may be valid but cannot be confidently interpreted from its text alone",
    ],
    DO_NOT_USE_FOR_TRAINING: [
        "valid operational record but unsuitable as a clear training example",
    ],
    REJECTED_GIBBERISH: [
        "meaningless, corrupted, test, or unusable message",
    ],
}


class Phase4ReviewError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewPaths:
    candidate_csv: Path = CANDIDATE_CSV
    decisions_csv: Path = DECISIONS_CSV
    merged_csv: Path = MERGED_REVIEW_CSV
    reports_dir: Path = REPORTS_DIR


def _bool_text(value: bool | str | None) -> str:
    if isinstance(value, str):
        return "true" if value.strip().lower() in {"1", "true", "yes", "y"} else "false"
    return "true" if value else "false"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv_atomic(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                extrasaction="ignore",
                lineterminator="\n",
                quoting=csv.QUOTE_MINIMAL,
            )
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def _file_lock(target: Path, timeout_seconds: float = 10.0):
    lock_path = target.with_suffix(target.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_seconds
    fd: Optional[int] = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
        except FileExistsError:
            if time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for review decision lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


def load_candidates(candidate_csv: Path = CANDIDATE_CSV) -> list[dict]:
    rows = _read_csv(candidate_csv)
    if not rows:
        raise Phase4ReviewError(f"No Phase 4 candidates found at {candidate_csv}")
    return rows


def load_decisions(decisions_csv: Path = DECISIONS_CSV) -> list[dict]:
    rows = _read_csv(decisions_csv)
    normalized = []
    for row in rows:
        base = DEFAULT_DECISION.copy()
        base.update({field: row.get(field, "") for field in DECISION_FIELDS})
        base["use_for_training"] = _bool_text(base["use_for_training"])
        normalized.append(base)
    return normalized


def candidate_ids(candidate_csv: Path = CANDIDATE_CSV) -> set[str]:
    return {row["candidate_id"] for row in load_candidates(candidate_csv)}


def _validate_decision(
    *,
    candidate_id: str,
    review_status: str,
    use_for_training: bool | str,
    reviewer: str,
    valid_candidate_ids: set[str],
) -> None:
    if candidate_id not in valid_candidate_ids:
        raise Phase4ReviewError(f"Unknown Phase 4 candidate_id: {candidate_id}")
    if review_status not in REVIEW_STATUSES:
        raise Phase4ReviewError(f"Unsupported review_status: {review_status}")
    if not reviewer.strip():
        raise Phase4ReviewError("Reviewer is required before saving a Phase 4 decision.")
    if _bool_text(use_for_training) == "true" and review_status not in TRAINING_COMPATIBLE_STATUSES:
        raise Phase4ReviewError(f"{review_status} cannot be marked use_for_training=true")


def _changed_fields(candidate: dict, corrected_fields: dict) -> dict:
    changed = {}
    for field, value in corrected_fields.items():
        if value in (None, ""):
            continue
        original_field = field.removeprefix("corrected_")
        if str(candidate.get(original_field, "")) != str(value):
            changed[original_field] = {"original": candidate.get(original_field, ""), "corrected": value}
    return changed


def save_decision(
    candidate_id: str,
    *,
    review_status: str,
    use_for_training: bool | str = False,
    reviewer: str,
    review_notes: str = "",
    corrected_fields: Optional[dict] = None,
    changed_fields_json: Optional[str] = None,
    candidate_csv: Path = CANDIDATE_CSV,
    decisions_csv: Path = DECISIONS_CSV,
    merged_csv: Optional[Path] = MERGED_REVIEW_CSV,
) -> dict:
    candidates = load_candidates(candidate_csv)
    by_candidate_id = {row["candidate_id"]: row for row in candidates}
    _validate_decision(
        candidate_id=candidate_id,
        review_status=review_status,
        use_for_training=use_for_training,
        reviewer=reviewer,
        valid_candidate_ids=set(by_candidate_id),
    )

    corrected_fields = corrected_fields or {}
    normalized_corrected = {field: str(corrected_fields.get(field, "") or "") for field in CORRECTED_FIELDS}
    if changed_fields_json is None:
        changed_fields_json = json.dumps(
            _changed_fields(by_candidate_id[candidate_id], normalized_corrected),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        json.loads(changed_fields_json or "{}")

    saved = DEFAULT_DECISION.copy()
    saved.update(normalized_corrected)
    saved.update(
        {
            "candidate_id": candidate_id,
            "review_status": review_status,
            "use_for_training": _bool_text(use_for_training),
            "reviewer": reviewer.strip(),
            "review_notes": review_notes or "",
            "reviewed_at": _utc_now(),
            "changed_fields_json": changed_fields_json or "{}",
        }
    )

    with _file_lock(decisions_csv):
        rows_by_id = {row["candidate_id"]: row for row in load_decisions(decisions_csv) if row.get("candidate_id")}
        rows_by_id[candidate_id] = saved
        rows = [rows_by_id[key] for key in sorted(rows_by_id)]
        _write_csv_atomic(decisions_csv, rows, DECISION_FIELDS)

    if merged_csv is not None:
        write_merged_export(candidate_csv=candidate_csv, decisions_csv=decisions_csv, merged_csv=merged_csv)
    return saved


def clear_decision(
    candidate_id: str,
    *,
    candidate_csv: Path = CANDIDATE_CSV,
    decisions_csv: Path = DECISIONS_CSV,
    merged_csv: Optional[Path] = MERGED_REVIEW_CSV,
) -> dict:
    valid_ids = candidate_ids(candidate_csv)
    if candidate_id not in valid_ids:
        raise Phase4ReviewError(f"Unknown Phase 4 candidate_id: {candidate_id}")
    with _file_lock(decisions_csv):
        rows = [row for row in load_decisions(decisions_csv) if row.get("candidate_id") != candidate_id]
        _write_csv_atomic(decisions_csv, rows, DECISION_FIELDS)
    if merged_csv is not None:
        write_merged_export(candidate_csv=candidate_csv, decisions_csv=decisions_csv, merged_csv=merged_csv)
    pending = DEFAULT_DECISION.copy()
    pending["candidate_id"] = candidate_id
    return pending


def decision_for(candidate_id: str, decisions: Iterable[dict]) -> dict:
    for row in decisions:
        if row.get("candidate_id") == candidate_id:
            return row
    pending = DEFAULT_DECISION.copy()
    pending["candidate_id"] = candidate_id
    return pending


def merge_candidates_and_decisions(
    *,
    candidate_csv: Path = CANDIDATE_CSV,
    decisions_csv: Path = DECISIONS_CSV,
) -> list[dict]:
    candidates = load_candidates(candidate_csv)
    decisions = {row["candidate_id"]: row for row in load_decisions(decisions_csv) if row.get("candidate_id")}
    merged = []
    for candidate in candidates:
        row = {field: candidate.get(field, "") for field in CANDIDATE_FIELDS}
        decision = DEFAULT_DECISION.copy()
        decision["candidate_id"] = candidate["candidate_id"]
        decision.update(decisions.get(candidate["candidate_id"], {}))
        row.update({field: decision.get(field, "") for field in DECISION_FIELDS})
        merged.append(row)
    return merged


def write_merged_export(
    *,
    candidate_csv: Path = CANDIDATE_CSV,
    decisions_csv: Path = DECISIONS_CSV,
    merged_csv: Path = MERGED_REVIEW_CSV,
) -> list[dict]:
    rows = merge_candidates_and_decisions(candidate_csv=candidate_csv, decisions_csv=decisions_csv)
    fieldnames = list(CANDIDATE_FIELDS)
    for field in DECISION_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)
    _write_csv_atomic(merged_csv, rows, fieldnames)
    return rows


def default_sort_key(row: dict) -> tuple[int, int, str]:
    kind = row.get("candidate_kind", "")
    phase3_ordered = row.get("phase3_is_ordered", "").lower() == "true"
    if kind == "STRICT_ORDERED_REVERSAL":
        group = 0
    elif phase3_ordered:
        group = 1
    elif kind == "PROSE_REVERSAL_CANDIDATE":
        group = 2
    elif kind == "STRUCTURAL_FALSE_POSITIVE_CANDIDATE":
        group = 4
    else:
        group = 3
    try:
        row_number = int(row.get("source_row_number") or 0)
    except ValueError:
        row_number = 0
    return (group, row_number, row.get("candidate_id", ""))


def attach_decisions(candidates: list[dict], decisions: list[dict]) -> list[dict]:
    by_id = {row["candidate_id"]: row for row in decisions if row.get("candidate_id")}
    rows = []
    for candidate in candidates:
        row = candidate.copy()
        decision = DEFAULT_DECISION.copy()
        decision["candidate_id"] = candidate["candidate_id"]
        decision.update(by_id.get(candidate["candidate_id"], {}))
        for field in DECISION_FIELDS:
            row[f"decision_{field}" if field in candidate else field] = decision.get(field, "")
        row["current_review_status"] = decision["review_status"]
        row["current_use_for_training"] = decision["use_for_training"]
        row["is_reviewed"] = str(decision["review_status"] != PENDING_REVIEW).lower()
        rows.append(row)
    return rows


def filter_candidates(
    rows: list[dict],
    *,
    review_status: str = "ALL",
    candidate_kind: str = "ALL",
    phase3_bundle_type: str = "ALL",
    parser_ordered: str = "ALL",
    parser_needs_review: str = "ALL",
    candidate_scope: str = "ALL",
    source_segment: str = "ALL",
    use_for_training: str = "ALL",
    reviewed: str = "ALL",
) -> list[dict]:
    def keep(row: dict) -> bool:
        if review_status != "ALL" and row.get("current_review_status") != review_status:
            return False
        if candidate_kind != "ALL" and row.get("candidate_kind") != candidate_kind:
            return False
        if phase3_bundle_type != "ALL" and row.get("phase3_bundle_type") != phase3_bundle_type:
            return False
        if parser_ordered != "ALL" and row.get("phase3_is_ordered", "").lower() != parser_ordered.lower():
            return False
        if parser_needs_review != "ALL" and row.get("phase3_needs_review", "").lower() != parser_needs_review.lower():
            return False
        if candidate_scope == "STRICT" and row.get("candidate_kind") != "STRICT_ORDERED_REVERSAL":
            return False
        if candidate_scope == "BROAD" and row.get("candidate_kind") == "STRICT_ORDERED_REVERSAL":
            return False
        if source_segment != "ALL" and row.get("segment_name") != source_segment and row.get("segment_id") != source_segment:
            return False
        if use_for_training != "ALL" and row.get("current_use_for_training", "").lower() != use_for_training.lower():
            return False
        if reviewed == "REVIEWED" and row.get("is_reviewed") != "true":
            return False
        if reviewed == "UNREVIEWED" and row.get("is_reviewed") != "false":
            return False
        return True

    return sorted([row for row in rows if keep(row)], key=default_sort_key)


def progress_summary(
    *,
    candidate_csv: Path = CANDIDATE_CSV,
    decisions_csv: Path = DECISIONS_CSV,
) -> dict:
    rows = merge_candidates_and_decisions(candidate_csv=candidate_csv, decisions_csv=decisions_csv)
    total = len(rows)
    reviewed_rows = [row for row in rows if row["review_status"] != PENDING_REVIEW]
    def count(status: str) -> int:
        return sum(1 for row in rows if row["review_status"] == status)

    training_ids = [row["candidate_id"] for row in rows if row["use_for_training"] == "true"]
    return {
        "total_candidates": total,
        "reviewed_candidates": len(reviewed_rows),
        "pending_candidates": count(PENDING_REVIEW),
        "confirmed_reversals": count(CONFIRMED_ORDERED_REVERSAL),
        "confirmed_other_multi_clause": count(CONFIRMED_MULTI_CLAUSE_REVIEW_ONLY),
        "false_positives": count(FALSE_POSITIVE),
        "needs_context": count(NEEDS_MORE_CONTEXT),
        "rejected_gibberish": count(REJECTED_GIBBERISH),
        "do_not_use_records": count(DO_NOT_USE_FOR_TRAINING),
        "training_eligible_records": len(training_ids),
        "progress_percentage": round((len(reviewed_rows) / total * 100), 2) if total else 0.0,
        "training_eligible_candidate_ids": training_ids,
        "reviewed_candidate_ids": [row["candidate_id"] for row in reviewed_rows],
        "pending_candidate_ids": [row["candidate_id"] for row in rows if row["review_status"] == PENDING_REVIEW],
    }


def write_review_reports(paths: ReviewPaths = ReviewPaths()) -> dict:
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    rows = merge_candidates_and_decisions(
        candidate_csv=paths.candidate_csv,
        decisions_csv=paths.decisions_csv,
    )
    summary = progress_summary(candidate_csv=paths.candidate_csv, decisions_csv=paths.decisions_csv)

    status_counts = {status: 0 for status in REVIEW_STATUSES}
    for row in rows:
        status_counts[row["review_status"]] = status_counts.get(row["review_status"], 0) + 1

    with (paths.reports_dir / SUMMARY_JSON.name).open("w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")

    _write_csv_atomic(
        paths.reports_dir / STATUS_COUNTS_CSV.name,
        [{"review_status": status, "count": count} for status, count in sorted(status_counts.items())],
        ["review_status", "count"],
    )
    _write_csv_atomic(
        paths.reports_dir / TRAINING_ELIGIBILITY_CSV.name,
        [
            {
                "candidate_id": row["candidate_id"],
                "review_status": row["review_status"],
                "use_for_training": row["use_for_training"],
            }
            for row in rows
            if row["use_for_training"] == "true"
        ],
        ["candidate_id", "review_status", "use_for_training"],
    )
    _write_csv_atomic(
        paths.reports_dir / PROGRESS_CSV.name,
        [{key: value for key, value in summary.items() if not key.endswith("_candidate_ids")}],
        [
            "total_candidates",
            "reviewed_candidates",
            "pending_candidates",
            "confirmed_reversals",
            "confirmed_other_multi_clause",
            "false_positives",
            "needs_context",
            "rejected_gibberish",
            "do_not_use_records",
            "training_eligible_records",
            "progress_percentage",
        ],
    )
    return summary
