"""Spotlight FTS via `mdfind` subprocess.

macOS already indexes the user's filesystem + mail + contacts + messages
+ calendar via Spotlight. Querying it via `mdfind` is free, fast, and
doesn't duplicate the index. We use it as the FTS surface for Layer 3 —
semantic queries hit Chroma, exact-match queries hit Spotlight.

On non-macOS, :func:`is_spotlight_available` returns False and queries
return ``[]``. V3 will plug in `tantivy` as a cross-platform fallback.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpotlightHit:
    """One result from `mdfind`."""

    path: str


def is_spotlight_available() -> bool:
    """Cheap probe — only checks that `mdfind` is on PATH."""
    return shutil.which("mdfind") is not None


def spotlight_query(
    query: str,
    *,
    only_in: str | None = None,
    max_results: int = 100,
    timeout_seconds: float = 5.0,
) -> list[SpotlightHit]:
    """Run a `mdfind` query and return result paths.

    ``only_in`` constrains the search to a directory subtree. ``max_results``
    caps the returned list (mdfind itself doesn't bound).
    """
    if not is_spotlight_available():
        return []
    cmd = ["mdfind"]
    if only_in:
        cmd += ["-onlyin", only_in]
    cmd.append(query)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    paths = [line for line in result.stdout.splitlines() if line.strip()]
    return [SpotlightHit(path=p) for p in paths[:max_results]]
