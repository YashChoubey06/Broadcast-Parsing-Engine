"""
scripts/inspect_dataset.py
===========================
Dataset inspection: label distribution, class balance, review flags.

    python scripts/inspect_dataset.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from src.config import DATA_DIR, PILOT_TRAIN_FILE, TRAIN_READY_FILE


def inspect(csv_file: str) -> None:
    path = DATA_DIR / csv_file
    if not path.exists():
        print(f"File not found: {path}")
        return
    df = pd.read_csv(path, low_memory=False)
    print(f"\n{'='*60}")
    print(f"File: {csv_file}")
    print(f"Rows: {len(df)}")
    if "action_label" in df.columns:
        print(f"\nAction label distribution:")
        counts = df["action_label"].value_counts()
        for label, cnt in counts.items():
            pct = 100 * cnt / len(df)
            print(f"  {label:<35} {cnt:>5}  ({pct:.1f}%)")
    if "needs_review" in df.columns:
        n_review = df["needs_review"].astype(str).str.lower().isin(["true", "1"]).sum()
        print(f"\nNeeds review: {n_review} / {len(df)}")
    if "auto_apply_eligible" in df.columns:
        n_auto = df["auto_apply_eligible"].astype(str).str.lower().isin(["true", "1"]).sum()
        print(f"Auto-apply eligible: {n_auto} / {len(df)}")


if __name__ == "__main__":
    inspect(TRAIN_READY_FILE)
    inspect(PILOT_TRAIN_FILE)
