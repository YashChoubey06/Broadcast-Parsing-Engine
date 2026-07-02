"""
Phase 4 raw reversal candidate recovery scanner.

This script reads the immutable raw broadcast CSV, verifies its fingerprint,
and writes a separate local human-review candidate dataset. It does not label,
train, replay, migrate, or apply holdings changes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from src.config import PROJECT_ROOT, REPORTS_DIR
from src.hybrid_parser import HybridParser
from src.schemas import ParsedMessageBundle, ParsedTradeEvent
from src.text_normalizer import normalize


EXPECTED_SOURCE = PROJECT_ROOT / "data" / "raw" / "broadcast_admin.broadcasts.csv"
EXPECTED_DATA_ROWS = 1704
EXPECTED_SHA256 = "824d76f16474c4dd08cf72e9382462169621510a4c861ed3bd941755f3ffad65"

DEFAULT_CANDIDATE_CSV = PROJECT_ROOT / "data" / "review" / "phase4_reversal_candidates.csv"
DEFAULT_MANIFEST_JSON = PROJECT_ROOT / "data" / "review" / "phase4_reversal_candidates_manifest.json"

SUMMARY_JSON = REPORTS_DIR / "phase4_reversal_candidate_summary.json"
REASON_COUNTS_CSV = REPORTS_DIR / "phase4_reversal_candidate_reason_counts.csv"
PARSER_COMPARISON_CSV = REPORTS_DIR / "phase4_reversal_parser_comparison.csv"
SOURCE_INTEGRITY_JSON = REPORTS_DIR / "phase4_reversal_source_integrity.json"

REQUIRED_HEADERS = [
    "_id",
    "segment",
    "segmentName",
    "text",
    "createdAt",
    "updatedAt",
]

CANDIDATE_FIELDS = [
    "candidate_id",
    "source_file",
    "source_sha256",
    "source_row_number",
    "source_message_id",
    "created_at",
    "updated_at",
    "segment_id",
    "segment_name",
    "raw_text",
    "normalized_text",
    "candidate_kind",
    "detected_separators",
    "detection_reasons",
    "actionable_clause_count",
    "clause_1_text",
    "clause_2_text",
    "extra_clauses_json",
    "phase3_bundle_type",
    "phase3_is_ordered",
    "phase3_child_count",
    "phase3_auto_apply_eligible",
    "phase3_needs_review",
    "phase3_validation_errors_json",
    "phase3_validation_warnings_json",
    "context_required",
    "context_validation_not_run",
    "context_validation_reason",
    "structural_parser_result",
    "child_1_json",
    "child_2_json",
    "child_1_surface_instruction",
    "child_2_surface_instruction",
    "child_1_symbol",
    "child_2_symbol",
    "child_1_position_effect",
    "child_2_position_effect",
    "child_1_entry_capacity_pct",
    "child_2_entry_capacity_pct",
    "same_instrument_identity",
    "review_status",
    "use_for_training",
    "reviewer",
    "review_notes",
    "reviewed_at",
]

_STRUCTURAL_SPLIT_RE = re.compile(r"\s*(?:&|;|\r?\n+|\\n)\s*")
_CLOSE_RE = re.compile(
    r"\b(FULL\s+PROFIT|BOOK\s+FULL\s+PROFIT|EXIT\s+FROM|SL\s+TOUCH(?:ED)?|STOP\s+LOSS\s+HIT)\b",
    re.IGNORECASE,
)
_ENTRY_RE = re.compile(r"\b(BUY|SELL)\b", re.IGNORECASE)
_ACTIONABLE_RE = re.compile(
    r"\b(BUY|SELL|FULL\s+PROFIT|BOOK\s+FULL\s+PROFIT|EXIT\s+FROM|SL\s+TOUCH(?:ED)?|STOP\s+LOSS\s+HIT|PART\s+PROFIT|PROFIT\s+BOOK|HOLD)\b",
    re.IGNORECASE,
)
_PROSE_EXIT_RE = re.compile(r"\bEXIT\s+FROM\b.{0,80}\b(POSITIONS|INDICES|STOCKS)\b", re.IGNORECASE)


class SourceIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceIntegrity:
    source_file: str
    source_sha256: str
    data_rows: int
    headers: list[str]


@dataclass(frozen=True)
class ScanTotals:
    source_sha256_before: str
    source_sha256_after: str
    source_data_rows: int
    strict_ordered_reversal_count: int
    prose_reversal_count: int
    other_multi_clause_candidate_count: int
    broad_review_count: int
    total_unique_candidate_count: int
    phase3_parser_ordered_bundle_count: int
    phase3_parser_review_count: int
    false_positive_candidate_count: int


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_rows(path: Path) -> tuple[list[dict], list[str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def verify_source_integrity(
    source_path: Path,
    *,
    expected_sha256: str = EXPECTED_SHA256,
    expected_rows: int = EXPECTED_DATA_ROWS,
    required_headers: Iterable[str] = REQUIRED_HEADERS,
) -> SourceIntegrity:
    source_path = source_path.resolve()
    if not source_path.exists():
        raise SourceIntegrityError(f"Source CSV not found: {source_path}")

    source_hash = _sha256(source_path)
    rows, headers = _read_rows(source_path)
    missing_headers = [header for header in required_headers if header not in headers]
    errors = []
    if source_hash.lower() != expected_sha256.lower():
        errors.append(f"SHA-256 mismatch: expected {expected_sha256}, got {source_hash}")
    if len(rows) != expected_rows:
        errors.append(f"Data-row count mismatch: expected {expected_rows}, got {len(rows)}")
    if missing_headers:
        errors.append(f"Missing required headers: {', '.join(missing_headers)}")
    if errors:
        raise SourceIntegrityError("; ".join(errors))

    return SourceIntegrity(
        source_file=str(source_path),
        source_sha256=source_hash,
        data_rows=len(rows),
        headers=headers,
    )


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _detected_separators(text: str) -> list[str]:
    separators = []
    if "&" in text:
        separators.append("&")
    if ";" in text:
        separators.append(";")
    if "\n" in text or "\r" in text:
        separators.append("newline")
    if "\\n" in text:
        separators.append("escaped_newline")
    return separators


def split_structural_clauses(raw_text: str) -> list[str]:
    return [part.strip() for part in _STRUCTURAL_SPLIT_RE.split(raw_text or "") if part.strip()]


def _actionable_clauses(clauses: list[str]) -> list[str]:
    return [clause for clause in clauses if _ACTIONABLE_RE.search(clause)]


def _looks_multi_instrument(bundle: ParsedMessageBundle) -> bool:
    symbols = {
        child.symbol
        for child in bundle.child_events
        if child.symbol
    }
    return len(symbols) > 1 or any(child.is_multi_instrument for child in bundle.child_events)


def _classify_candidate(
    raw_text: str,
    clauses: list[str],
    actionable: list[str],
    bundle: ParsedMessageBundle,
) -> tuple[Optional[str], list[str], str]:
    separators = _detected_separators(raw_text)
    has_close = bool(_CLOSE_RE.search(raw_text or "") or _PROSE_EXIT_RE.search(raw_text or ""))
    has_entry = bool(_ENTRY_RE.search(raw_text or ""))
    has_structural = bool(separators)
    multi_instrument = _looks_multi_instrument(bundle)
    reasons: list[str] = []

    if has_close and has_entry and not has_structural:
        reasons.append("prose_close_plus_entry")
        return "PROSE_REVERSAL_CANDIDATE", reasons, "STRUCTURAL_REVIEW_REQUIRED"

    if not has_structural:
        return None, [], "NOT_A_PHASE4_CANDIDATE"

    if has_close and has_entry:
        reasons.append("strict_close_plus_entry")
    if multi_instrument:
        reasons.append("multiple_instruments_detected")
    if len(actionable) > 2:
        reasons.append("more_than_two_actionable_clauses")
        return "TOO_MANY_ACTIONABLE_CLAUSES", reasons, "TOO_MANY_ACTIONABLE_CLAUSES"
    if len(actionable) == 2 and has_close and has_entry and not multi_instrument:
        return "STRICT_ORDERED_REVERSAL", reasons, "STRUCTURAL_ORDERED_REVERSAL"
    if multi_instrument:
        return "MULTI_INSTRUMENT_CANDIDATE", reasons, "STRUCTURAL_MULTI_INSTRUMENT"
    if len(actionable) >= 2:
        reasons.append("multiple_actionable_clauses")
        return "MULTI_CLAUSE_TRADE", reasons, "STRUCTURAL_MULTI_CLAUSE"
    if len(actionable) == 1:
        reasons.append("separator_with_single_actionable_clause")
        return "STRUCTURAL_FALSE_POSITIVE_CANDIDATE", reasons, "STRUCTURAL_FALSE_POSITIVE"
    return None, [], "NOT_A_PHASE4_CANDIDATE"


def _candidate_id(source_sha256: str, row_number: int, source_message_id: str, normalized_text: str) -> str:
    stable = "|".join([source_sha256.lower(), str(row_number), source_message_id or "", normalized_text])
    return "rev4_" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def _event_json(child: Optional[ParsedTradeEvent]) -> str:
    if child is None:
        return ""
    return _json_dumps(child.to_dict())


def _same_identity(child1: Optional[ParsedTradeEvent], child2: Optional[ParsedTradeEvent]) -> str:
    if not child1 or not child2 or not child1.symbol or not child2.symbol:
        return ""
    keys = ("market_group", "symbol", "contract_month", "option_type", "strike_price")
    same = all(str(getattr(child1, key) or "") == str(getattr(child2, key) or "") for key in keys)
    return "true" if same else "false"


def _build_candidate_row(
    *,
    source_path: Path,
    source_sha256: str,
    row_number: int,
    row: dict,
    parser: HybridParser,
) -> Optional[dict]:
    raw_text = row.get("text") or ""
    normalized_text = normalize(raw_text)
    bundle = parser.parse_bundle(
        raw_text,
        source_message_id=row.get("_id") or None,
        existing_long=False,
        existing_short=False,
        has_holdings_context=False,
        market_group=row.get("segmentName") or None,
    )
    clauses = split_structural_clauses(raw_text)
    actionable = _actionable_clauses(clauses)
    candidate_kind, reasons, structural_result = _classify_candidate(raw_text, clauses, actionable, bundle)
    if not candidate_kind:
        return None

    child1 = bundle.child_events[0] if len(bundle.child_events) >= 1 else None
    child2 = bundle.child_events[1] if len(bundle.child_events) >= 2 else None
    candidate_id = _candidate_id(source_sha256, row_number, row.get("_id") or "", normalized_text)
    extra_clauses = actionable[2:] if len(actionable) > 2 else clauses[2:] if len(clauses) > 2 else []
    context_reason = "Raw CSV scan has no trustworthy reconstructed position state."

    return {
        "candidate_id": candidate_id,
        "source_file": source_path.as_posix(),
        "source_sha256": source_sha256,
        "source_row_number": str(row_number),
        "source_message_id": row.get("_id") or "",
        "created_at": row.get("createdAt") or "",
        "updated_at": row.get("updatedAt") or "",
        "segment_id": row.get("segment") or "",
        "segment_name": row.get("segmentName") or "",
        "raw_text": raw_text,
        "normalized_text": normalized_text,
        "candidate_kind": candidate_kind,
        "detected_separators": _json_dumps(_detected_separators(raw_text)),
        "detection_reasons": _json_dumps(reasons),
        "actionable_clause_count": str(len(actionable)),
        "clause_1_text": actionable[0] if len(actionable) >= 1 else "",
        "clause_2_text": actionable[1] if len(actionable) >= 2 else "",
        "extra_clauses_json": _json_dumps(extra_clauses),
        "phase3_bundle_type": bundle.bundle_type,
        "phase3_is_ordered": "true" if bundle.is_ordered else "false",
        "phase3_child_count": str(len(bundle.child_events)),
        "phase3_auto_apply_eligible": "true" if bundle.auto_apply_eligible else "false",
        "phase3_needs_review": "true" if bundle.needs_review else "false",
        "phase3_validation_errors_json": _json_dumps(bundle.validation_errors),
        "phase3_validation_warnings_json": _json_dumps(bundle.validation_warnings),
        "context_required": "true",
        "context_validation_not_run": "true",
        "context_validation_reason": context_reason,
        "structural_parser_result": structural_result,
        "child_1_json": _event_json(child1),
        "child_2_json": _event_json(child2),
        "child_1_surface_instruction": child1.surface_instruction if child1 else "",
        "child_2_surface_instruction": child2.surface_instruction if child2 else "",
        "child_1_symbol": child1.symbol if child1 else "",
        "child_2_symbol": child2.symbol if child2 else "",
        "child_1_position_effect": child1.position_effect if child1 else "",
        "child_2_position_effect": child2.position_effect if child2 else "",
        "child_1_entry_capacity_pct": str(child1.entry_capacity_pct) if child1 and child1.entry_capacity_pct is not None else "",
        "child_2_entry_capacity_pct": str(child2.entry_capacity_pct) if child2 and child2.entry_capacity_pct is not None else "",
        "same_instrument_identity": _same_identity(child1, child2),
        "review_status": "PENDING_REVIEW",
        "use_for_training": "false",
        "reviewer": "",
        "review_notes": "",
        "reviewed_at": "",
    }


def _write_candidate_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=CANDIDATE_FIELDS,
            extrasaction="ignore",
            lineterminator="\n",
            quoting=csv.QUOTE_MINIMAL,
        )
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")


def _write_reason_counts(path: Path, rows: list[dict]) -> None:
    counts = Counter()
    for row in rows:
        for reason in json.loads(row["detection_reasons"]):
            counts[reason] += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["reason", "count"], lineterminator="\n")
        writer.writeheader()
        for reason, count in sorted(counts.items()):
            writer.writerow({"reason": reason, "count": count})


def _write_parser_comparison(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "candidate_id",
        "candidate_kind",
        "phase3_bundle_type",
        "phase3_is_ordered",
        "phase3_child_count",
        "phase3_auto_apply_eligible",
        "phase3_needs_review",
        "phase3_validation_errors_json",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in fields})


def _totals(source_before: str, source_after: str, data_rows: int, rows: list[dict]) -> ScanTotals:
    kind_counts = Counter(row["candidate_kind"] for row in rows)
    parser_ordered = sum(1 for row in rows if row["phase3_is_ordered"] == "true")
    parser_review = sum(1 for row in rows if row["phase3_needs_review"] == "true")
    return ScanTotals(
        source_sha256_before=source_before,
        source_sha256_after=source_after,
        source_data_rows=data_rows,
        strict_ordered_reversal_count=kind_counts["STRICT_ORDERED_REVERSAL"],
        prose_reversal_count=kind_counts["PROSE_REVERSAL_CANDIDATE"],
        other_multi_clause_candidate_count=sum(
            count
            for kind, count in kind_counts.items()
            if kind not in {"STRICT_ORDERED_REVERSAL", "PROSE_REVERSAL_CANDIDATE", "STRUCTURAL_FALSE_POSITIVE_CANDIDATE"}
        ),
        broad_review_count=sum(1 for row in rows if row["candidate_kind"] != "STRICT_ORDERED_REVERSAL"),
        total_unique_candidate_count=len(rows),
        phase3_parser_ordered_bundle_count=parser_ordered,
        phase3_parser_review_count=parser_review,
        false_positive_candidate_count=kind_counts["STRUCTURAL_FALSE_POSITIVE_CANDIDATE"],
    )


def run_scan(
    *,
    source_path: Path = EXPECTED_SOURCE,
    candidate_csv: Path = DEFAULT_CANDIDATE_CSV,
    manifest_json: Path = DEFAULT_MANIFEST_JSON,
    reports_dir: Path = REPORTS_DIR,
    expected_sha256: str = EXPECTED_SHA256,
    expected_rows: int = EXPECTED_DATA_ROWS,
    parser: Optional[HybridParser] = None,
) -> ScanTotals:
    source_path = source_path.resolve()
    integrity = verify_source_integrity(
        source_path,
        expected_sha256=expected_sha256,
        expected_rows=expected_rows,
    )
    source_hash_before = integrity.source_sha256
    source_rows, _headers = _read_rows(source_path)
    parser = parser or HybridParser()

    candidate_rows = []
    seen_ids = set()
    for index, row in enumerate(source_rows, start=1):
        candidate = _build_candidate_row(
            source_path=source_path,
            source_sha256=source_hash_before,
            row_number=index,
            row=row,
            parser=parser,
        )
        if candidate and candidate["candidate_id"] not in seen_ids:
            candidate_rows.append(candidate)
            seen_ids.add(candidate["candidate_id"])

    candidate_rows.sort(key=lambda item: (int(item["source_row_number"]), item["candidate_id"]))
    source_hash_after = _sha256(source_path)
    totals = _totals(source_hash_before, source_hash_after, integrity.data_rows, candidate_rows)

    manifest = {
        "candidate_count": len(candidate_rows),
        "candidate_ids": [row["candidate_id"] for row in candidate_rows],
        "candidate_schema": CANDIDATE_FIELDS,
        "context_validation_not_run": True,
        "context_validation_reason": "Raw CSV scan has no trustworthy reconstructed position state.",
        "outputs": {
            "candidate_csv": str(candidate_csv),
            "manifest_json": str(manifest_json),
        },
        "source": {
            "data_rows": integrity.data_rows,
            "headers": integrity.headers,
            "source_file": integrity.source_file,
            "source_sha256_before": source_hash_before,
            "source_sha256_after": source_hash_after,
        },
        "totals": totals.__dict__,
    }

    summary_path = reports_dir / SUMMARY_JSON.name
    reason_counts_path = reports_dir / REASON_COUNTS_CSV.name
    parser_comparison_path = reports_dir / PARSER_COMPARISON_CSV.name
    source_integrity_path = reports_dir / SOURCE_INTEGRITY_JSON.name

    _write_candidate_csv(candidate_csv, candidate_rows)
    _write_json(manifest_json, manifest)
    _write_json(summary_path, {"totals": totals.__dict__, "candidate_ids": [row["candidate_id"] for row in candidate_rows]})
    _write_reason_counts(reason_counts_path, candidate_rows)
    _write_parser_comparison(parser_comparison_path, candidate_rows)
    _write_json(source_integrity_path, manifest["source"])

    return totals


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Recover Phase 4 reversal candidates for human review.")
    parser.add_argument("--source", type=Path, default=EXPECTED_SOURCE)
    parser.add_argument("--candidate-csv", type=Path, default=DEFAULT_CANDIDATE_CSV)
    parser.add_argument("--manifest-json", type=Path, default=DEFAULT_MANIFEST_JSON)
    parser.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    args = parser.parse_args(argv)

    try:
        totals = run_scan(
            source_path=args.source,
            candidate_csv=args.candidate_csv,
            manifest_json=args.manifest_json,
            reports_dir=args.reports_dir,
        )
    except SourceIntegrityError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print("Phase 4 candidate scan complete.")
    print(f"source_sha256_before={totals.source_sha256_before}")
    print(f"source_sha256_after={totals.source_sha256_after}")
    print(f"source_data_rows={totals.source_data_rows}")
    print(f"strict_ordered_reversal_count={totals.strict_ordered_reversal_count}")
    print(f"prose_reversal_count={totals.prose_reversal_count}")
    print(f"other_multi_clause_candidate_count={totals.other_multi_clause_candidate_count}")
    print(f"broad_review_count={totals.broad_review_count}")
    print(f"total_unique_candidate_count={totals.total_unique_candidate_count}")
    print(f"phase3_parser_ordered_bundle_count={totals.phase3_parser_ordered_bundle_count}")
    print(f"phase3_parser_review_count={totals.phase3_parser_review_count}")
    print(f"false_positive_candidate_count={totals.false_positive_candidate_count}")
    print(f"candidate_csv={args.candidate_csv}")
    print(f"manifest_json={args.manifest_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
