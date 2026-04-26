"""Date-versioned release helper. Format: YYYY.M.D (no zero-padding).

Why date versions: ship-when-ready cadence beats semver theatre for an
internal-first framework. The only invariant is that pyproject.toml,
__version__, and the git tag agree.
"""
from __future__ import annotations

from datetime import date
from importlib import metadata


def current_version() -> str:
    """Installed package version (from importlib.metadata)."""
    return metadata.version("opencomputer")


def parse_date_version(s: str) -> tuple[int, int, int]:
    """Parse 'YYYY.M.D' → (year, month, day). Raises ValueError on any other format."""
    parts = s.split(".")
    if len(parts) != 3:
        raise ValueError(f"Not a YYYY.M.D version: {s!r}")
    try:
        y, m, d = (int(p) for p in parts)
    except ValueError as e:
        raise ValueError(f"Not a YYYY.M.D version: {s!r}") from e
    date(y, m, d)
    return y, m, d


def today_version() -> str:
    """Today's date as a YYYY.M.D version string (no zero-padding)."""
    t = date.today()
    return f"{t.year}.{t.month}.{t.day}"
