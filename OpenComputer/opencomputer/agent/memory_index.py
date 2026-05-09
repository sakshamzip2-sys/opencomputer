"""BM25 retrieval index over MEMORY.md (v1.1 plan-3 M6.1).

Profile-scoped, lazily built, cache-backed.  See
``docs/superpowers/specs/2026-05-09-v1-1-m6-1-bm25-index-design.md``
for the full design rationale.

Public API:
    - BM25Index(profile_home: Path)
    - BM25Index.query(text, top_k) -> list[QueryHit]
    - BM25Index.invalidate() -> None

The cache file lives at ``<profile_home>/cache/memory_bm25.idx`` and
self-validates on load (format_version + corpus sha256 + rank_bm25 version).
A mismatch triggers a transparent rebuild, never a silent stale-result return.
Cache failures are logged at WARNING; the rebuild path is taken silently
from the caller's perspective.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

from rank_bm25 import BM25Okapi

logger = logging.getLogger("opencomputer.agent.memory_index")

# Pickle protocol 5 is universally available on Python 3.12+ (the project's
# minimum interpreter) and is forward-compatible with newer Pythons.  Pinning
# to a stable protocol means a cache file written by one Python version is
# readable by another, removing one common cache-bust trigger.
PICKLE_PROTOCOL: int = 5


def _resolve_rank_bm25_version() -> str:
    """rank_bm25 has no public ``__version__`` attribute on every release.

    Resolve via ``importlib.metadata`` so the cache header records the
    installed wheel version regardless.  Returns ``"unknown"`` only if the
    distribution metadata is missing entirely (very unusual; would indicate
    a hand-installed source tree).  An ``"unknown"`` value still works —
    cache invalidates whenever the recorded value changes from the previous
    build, which means the first cache built with metadata-resolved version
    will not load against a previous "unknown" build.  Acceptable: rebuild
    is sub-second.
    """
    try:
        return _pkg_version("rank_bm25")
    except PackageNotFoundError:
        return "unknown"


_RANK_BM25_VERSION: str = _resolve_rank_bm25_version()


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
        self._bm25: BM25Okapi | None = None
        self._loaded: bool = False

    # ─── public ────────────────────────────────────────────────────────

    def query(self, text: str, top_k: int = 5) -> list[QueryHit]:
        if not self._loaded:
            if not self._load_cache():
                self._build()
                if self._entries:  # only persist non-empty corpora
                    self._save_cache()

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
        self._entries = []
        self._tokens = []
        self._bm25 = None
        self._loaded = False
        try:
            self._cache_path.unlink()
        except FileNotFoundError:
            pass
        # also clean up any stale .tmp from a crashed save
        tmp_path = self._cache_path.with_suffix(".tmp")
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

    # ─── cache I/O ─────────────────────────────────────────────────────

    def _current_sha256(self) -> str:
        if not self._memory_path.exists():
            return hashlib.sha256(b"").hexdigest()
        return hashlib.sha256(self._memory_path.read_bytes()).hexdigest()

    def _load_cache(self) -> bool:
        if not self._cache_path.exists():
            return False
        try:
            with self._cache_path.open("rb") as f:
                data = pickle.load(f)
            header = data["header"]
            if not isinstance(header, dict):
                logger.warning(
                    "BM25 cache header malformed at %s; rebuilding", self._cache_path
                )
                return False
            if header.get("format_version") != self.FORMAT_VERSION:
                logger.info(
                    "BM25 cache format_version=%r != expected %d at %s; rebuilding",
                    header.get("format_version"),
                    self.FORMAT_VERSION,
                    self._cache_path,
                )
                return False
            if header.get("rank_bm25_version") != _RANK_BM25_VERSION:
                logger.info(
                    "BM25 cache rank_bm25_version=%r != current %s at %s; rebuilding",
                    header.get("rank_bm25_version"),
                    _RANK_BM25_VERSION,
                    self._cache_path,
                )
                return False
            if header.get("corpus_sha256") != self._current_sha256():
                logger.debug(
                    "BM25 cache corpus_sha256 mismatch at %s; rebuilding",
                    self._cache_path,
                )
                return False
            entries = data["entries"]
            tokens = data["tokens"]
            bm25 = data["bm25"]
            if not isinstance(entries, list) or not isinstance(tokens, list):
                logger.warning(
                    "BM25 cache entries/tokens shape unexpected at %s; rebuilding",
                    self._cache_path,
                )
                return False
            if not isinstance(bm25, BM25Okapi):
                logger.warning(
                    "BM25 cache bm25 object not a BM25Okapi at %s; rebuilding",
                    self._cache_path,
                )
                return False
        except (
            pickle.UnpicklingError,
            KeyError,
            EOFError,
            OSError,
            AttributeError,
            ValueError,
            ImportError,
            TypeError,
        ) as exc:
            logger.warning(
                "BM25 cache load failed (%s: %s) at %s; rebuilding",
                type(exc).__name__,
                exc,
                self._cache_path,
            )
            return False

        self._entries = entries
        self._tokens = tokens
        self._bm25 = bm25
        self._loaded = True
        return True

    def _save_cache(self) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "BM25 cache dir create failed (%s); retrieval will rebuild on next call",
                exc,
            )
            return

        data = {
            "header": {
                "format_version": self.FORMAT_VERSION,
                "rank_bm25_version": _RANK_BM25_VERSION,
                "corpus_sha256": self._current_sha256(),
                "entry_count": len(self._entries),
                "mtime_ns": (
                    self._memory_path.stat().st_mtime_ns
                    if self._memory_path.exists()
                    else 0
                ),
                "built_at": datetime.now(tz=UTC).isoformat(),
            },
            "entries": self._entries,
            "tokens": self._tokens,
            "bm25": self._bm25,
        }
        tmp_path = self._cache_path.with_suffix(".tmp")
        try:
            with tmp_path.open("wb") as f:
                pickle.dump(data, f, protocol=PICKLE_PROTOCOL)
            os.replace(tmp_path, self._cache_path)
        except OSError as exc:
            # disk full / permission denied / etc.  Retrieval still works
            # from in-memory state; next process will rebuild.  Don't raise.
            logger.warning(
                "BM25 cache save failed (%s); next call will rebuild", exc
            )
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

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
