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

from rank_bm25 import BM25Okapi


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
        if not self._loaded:
            self._build()

        if not self._entries or self._bm25 is None:
            return []

        query_tokens = self._tokenize(text)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)
        ranked = sorted(
            ((float(score), i) for i, score in enumerate(scores) if score > 0),
            key=lambda pair: pair[0],
            reverse=True,
        )
        hits: list[QueryHit] = []
        for rank, (score, idx) in enumerate(ranked[:top_k]):
            hits.append(QueryHit(entry=self._entries[idx], score=score, rank=rank))
        return hits

    def invalidate(self) -> None:
        raise NotImplementedError  # implemented in Task 6

    # ─── build ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        """Read MEMORY.md, segment, tokenize, build BM25 in memory."""
        if not self._memory_path.exists():
            self._entries = []
            self._tokens = []
            self._bm25 = None
            self._loaded = True
            return

        text = self._memory_path.read_text(encoding="utf-8")
        self._entries = self._segment(text)
        self._tokens = [self._tokenize(e.raw) for e in self._entries]
        if self._tokens and any(self._tokens):
            self._bm25 = BM25Okapi(self._tokens)
        else:
            self._bm25 = None
        self._loaded = True

    # ─── tokenization (pure) ───────────────────────────────────────────

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return BM25Index._TOKEN_RE.findall(text.lower())

    # ─── segmentation (pure) ───────────────────────────────────────────

    _HEADING_RE = re.compile(r"^#{1,6}\s")

    @staticmethod
    def _segment(text: str) -> list[IndexedEntry]:
        if not text or not text.strip():
            return []

        lines = text.splitlines()
        entries: list[IndexedEntry] = []
        cur_lines: list[tuple[int, str]] = []  # (1-indexed line no, content)
        cur_blank_run = 0

        def flush() -> None:
            if not cur_lines:
                return
            line_start = cur_lines[0][0]
            line_end = cur_lines[-1][0]
            raw = "\n".join(content for _, content in cur_lines).strip()
            if raw:
                entries.append(IndexedEntry(raw=raw, line_start=line_start, line_end=line_end))
            cur_lines.clear()

        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                cur_blank_run += 1
                if cur_blank_run >= 1:
                    flush()
                continue

            cur_blank_run = 0

            if BM25Index._HEADING_RE.match(line):
                # heading is a strong boundary; flush prior entry then start a new one with the heading
                flush()
                cur_lines.append((idx, line))
                continue

            cur_lines.append((idx, line))

        flush()
        return entries
