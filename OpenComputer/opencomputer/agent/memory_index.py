"""BM25 retrieval index over MEMORY.md (v1.1 plan-3 M6.1).

Profile-scoped, lazily built, cache-backed.  See
``docs/superpowers/specs/2026-05-09-v1-1-m6-1-bm25-index-design.md``
for the full design rationale.

Public API:
    - BM25Index(profile_home: Path)
    - BM25Index.query(text, top_k) -> list[QueryHit]
    - BM25Index.invalidate() -> None

The cache file lives at ``<profile_home>/cache/memory_bm25.idx`` and
self-validates on load (format_version + corpus sha256).  A mismatch
triggers a transparent rebuild, never a silent stale-result return.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IndexedEntry:
    """One paragraph-delimited entry from MEMORY.md."""

    raw: str
    line_start: int  # 1-indexed, first non-blank line of the entry
    line_end: int    # 1-indexed, last non-blank line of the entry


@dataclass(frozen=True)
class QueryHit:
    entry: IndexedEntry
    score: float
    rank: int  # 0-indexed


class BM25Index:
    FORMAT_VERSION: int = 1
    CACHE_FILENAME: str = "memory_bm25.idx"

    _TOKEN_RE = re.compile(r"[a-z0-9]+")

    def __init__(self, profile_home: Path) -> None:
        self._profile_home = Path(profile_home)
        self._memory_path = self._profile_home / "MEMORY.md"
        self._cache_dir = self._profile_home / "cache"
        self._cache_path = self._cache_dir / self.CACHE_FILENAME

        self._entries: list[IndexedEntry] = []
        self._tokens: list[list[str]] = []
        self._bm25: object | None = None  # rank_bm25.BM25Okapi at runtime
        self._loaded: bool = False

    # ─── public ────────────────────────────────────────────────────────

    def query(self, text: str, top_k: int = 5) -> list[QueryHit]:
        raise NotImplementedError  # implemented in Task 4

    def invalidate(self) -> None:
        raise NotImplementedError  # implemented in Task 6

    # ─── tokenization (pure) ───────────────────────────────────────────

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return BM25Index._TOKEN_RE.findall(text.lower())
