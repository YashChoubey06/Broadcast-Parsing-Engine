from __future__ import annotations

import argparse
from pathlib import Path

from src.phase4_review import (
    CANDIDATE_CSV,
    DECISIONS_CSV,
    MERGED_REVIEW_CSV,
    REPORTS_DIR,
    ReviewPaths,
    write_merged_export,
    write_review_reports,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Phase 4 human-review progress reports.")
    parser.add_argument("--candidate-csv", type=Path, default=CANDIDATE_CSV)
    parser.add_argument("--decisions-csv", type=Path, default=DECISIONS_CSV)
    parser.add_argument("--merged-csv", type=Path, default=MERGED_REVIEW_CSV)
    parser.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    args = parser.parse_args(argv)

    write_merged_export(
        candidate_csv=args.candidate_csv,
        decisions_csv=args.decisions_csv,
        merged_csv=args.merged_csv,
    )
    summary = write_review_reports(
        ReviewPaths(
            candidate_csv=args.candidate_csv,
            decisions_csv=args.decisions_csv,
            merged_csv=args.merged_csv,
            reports_dir=args.reports_dir,
        )
    )

    print("Phase 4 human review summary complete.")
    print(f"total_candidates={summary['total_candidates']}")
    print(f"reviewed_candidates={summary['reviewed_candidates']}")
    print(f"pending_candidates={summary['pending_candidates']}")
    print(f"training_eligible_records={summary['training_eligible_records']}")
    print(f"progress_percentage={summary['progress_percentage']}")
    print(f"merged_csv={args.merged_csv}")
    print(f"reports_dir={args.reports_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
