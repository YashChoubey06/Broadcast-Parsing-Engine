"""
alias_repository.py
====================
Loads instrument aliases from  data/10_instrument_aliases.csv  and
resolves raw symbol text to canonical symbols.

Key design choices:
 - Aliases are sorted longest-first to avoid matching "NIFTY" inside "BANK NIFTY".
 - Matching is case-insensitive (both the alias table and the input are uppercased).
 - The CSV is loaded once and cached; pass an explicit path for testing.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from src.config import DATA_DIR, ALIASES_FILE


class AliasRepository:
    """
    Resolves raw instrument text to a canonical symbol using a loaded
    alias dictionary.

    Usage::

        repo = AliasRepository()
        canonical = repo.resolve("crude mini")   # → "CRUDE_MINI"
        canonical = repo.resolve("BANKNIFTY")    # → "BANKNIFTY"
    """

    def __init__(self, csv_path: Optional[Path] = None) -> None:
        path = csv_path or (DATA_DIR / ALIASES_FILE)
        self._alias_to_canonical: dict[str, str] = {}
        self._sorted_aliases: list[str] = []  # sorted longest-first
        self._load(path)

    def _load(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(
                f"Alias file not found: {path}\n"
                "Make sure data/10_instrument_aliases.csv is present."
            )
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                alias = row["observed_alias"].strip().upper()
                canonical = row["canonical_symbol"].strip().upper()
                if alias and canonical:
                    self._alias_to_canonical[alias] = canonical

        # Sort longest first so "BANK NIFTY" is matched before "NIFTY"
        self._sorted_aliases = sorted(
            self._alias_to_canonical.keys(), key=len, reverse=True
        )

    def resolve(self, raw: str) -> Optional[str]:
        """
        Return the canonical symbol for *raw*, or *None* if not found.
        """
        key = raw.strip().upper()
        return self._alias_to_canonical.get(key)

    def find_in_text(self, text: str) -> list[tuple[str, str, int, int]]:
        """
        Scan *text* (uppercased internally) for all known aliases.

        Returns a list of (alias, canonical, start, end) tuples,
        sorted by position in the text.  Longest aliases are tried first
        so overlapping shorter matches are suppressed.
        """
        upper = text.upper()
        found: list[tuple[str, str, int, int]] = []
        covered: set[int] = set()

        for alias in self._sorted_aliases:
            start = 0
            while True:
                idx = upper.find(alias, start)
                if idx == -1:
                    break
                end = idx + len(alias)

                # Check boundary – alias must be surrounded by non-word chars
                before_ok = idx == 0 or not upper[idx - 1].isalnum()
                after_ok = end == len(upper) or not upper[end].isalnum()

                if before_ok and after_ok:
                    # Suppress if all positions already covered by a longer match
                    positions = set(range(idx, end))
                    if not positions.intersection(covered):
                        found.append((alias, self._alias_to_canonical[alias], idx, end))
                        covered.update(positions)

                start = idx + 1

        found.sort(key=lambda x: x[2])  # sort by start position
        return found

    @property
    def all_aliases(self) -> list[str]:
        return list(self._sorted_aliases)

    @property
    def all_canonicals(self) -> set[str]:
        return set(self._alias_to_canonical.values())


# ---------------------------------------------------------------------------
# Module-level singleton (loaded lazily on first use)
# ---------------------------------------------------------------------------
_default_repo: Optional[AliasRepository] = None


def get_alias_repository() -> AliasRepository:
    global _default_repo
    if _default_repo is None:
        _default_repo = AliasRepository()
    return _default_repo
