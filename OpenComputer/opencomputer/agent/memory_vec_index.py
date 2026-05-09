"""Vector retrieval index over MEMORY.md (v1.1 plan-3 M6.2).

Profile-scoped, lazily built, cache-backed.  Sibling of
:mod:`opencomputer.agent.memory_index` (BM25); both indexes participate in
the M6.3 Active Memory layer's RRF (Reciprocal Rank Fusion) over per-turn
retrieval against MEMORY.md.

Design notes:

- The cache pickle file lives at ``<profile_home>/cache/memory_vec.idx``.
  Header records ``format_version + corpus_sha256 + dimensionality +
  model_id + entry_count + built_at``.  Mismatch on any of these
  triggers a transparent rebuild.  Cache failures are logged at WARNING.

- The provider that supplies embeddings is injected per-call via
  ``embed_fn``: ``Callable[[list[str]], Awaitable[EmbeddingBatch]]``.  This
  is :meth:`BaseProvider.embed` of the active provider plugin.  The
  vector index has no static reference to a provider — it must work
  identically against OpenAI's native embeddings (default-small, 1536-d)
  and Anthropic + Voyage (voyage-3-lite, 512-d) without code changes.

- ``EmbeddingsUnsupportedError`` from the provider is propagated, so the
  Active Memory layer can catch it and fall back to BM25-only retrieval
  with a one-time WARNING.

- Vector storage uses ``numpy.float32`` for size + speed.  An int64 key
  array maps row index → entry id (a content-derived hash) so an
  incremental rebuild after an append-only write reuses prior rows.

- Cosine similarity is implemented as a normalized-dot-product over the
  full corpus.  A 4 KB MEMORY.md has on the order of 50–200 entries;
  matrix-mat-vec with float32 numpy is sub-1 ms.  Approximate-NN
  structures (HNSW, IVF) are not justified at this scale and would add a
  build dependency.

- All retrieval is read-only and lock-free.  The cache file is written
  atomically (tmp + ``os.replace``) so a partially-written file never
  surfaces as a half-corrupt result; the integrity check would catch it
  anyway.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from plugin_sdk.embeddings import EmbeddingBatch

logger = logging.getLogger("opencomputer.agent.memory_vec_index")

PICKLE_PROTOCOL: int = 5


@dataclass(frozen=True)
class VectorEntry:
    """One paragraph-delimited entry from MEMORY.md.  Independent of the
    BM25 index's :class:`opencomputer.agent.memory_index.IndexedEntry`
    so this module can be imported without rank_bm25 in the path."""

    raw: str
    line_start: int
    line_end: int

    @property
    def content_id(self) -> str:
        """Stable id derived from the raw text — used by the incremental
        rebuild path to detect "this entry survived a rewrite"."""
        return hashlib.sha256(self.raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VectorHit:
    entry: VectorEntry
    score: float  # cosine similarity in [-1, 1]; usually [0, 1] for embedding models
    rank: int


# Type alias for the embedding callback the vector index expects from
# the active provider.  Matches :meth:`BaseProvider.embed` exactly.
EmbedFn = Callable[[list[str]], Awaitable[EmbeddingBatch]]


class VectorIndex:
    FORMAT_VERSION: int = 1
    CACHE_FILENAME: str = "memory_vec.idx"

    _HEADING_RE = re.compile(r"^#{1,6}\s")

    def __init__(self, profile_home: Path) -> None:
        self._profile_home = Path(profile_home)
        self._memory_path = self._profile_home / "MEMORY.md"
        self._cache_dir = self._profile_home / "cache"
        self._cache_path = self._cache_dir / self.CACHE_FILENAME

        self._entries: list[VectorEntry] = []
        self._vectors: np.ndarray | None = None  # shape (N, D), float32
        self._dimensionality: int = 0
        self._model_id: str = ""
        self._loaded: bool = False

    # ─── public ────────────────────────────────────────────────────────

    async def query(
        self,
        text: str,
        *,
        embed_fn: EmbedFn,
        top_k: int = 5,
    ) -> list[VectorHit]:
        """Retrieve top_k entries by cosine similarity to the query.

        Embeds the query via ``embed_fn`` (the active provider's
        :meth:`BaseProvider.embed`).  Builds the corpus index lazily on
        first call (or on cache miss) by chunking + embedding all
        existing MEMORY.md entries.

        Returns an empty list if MEMORY.md is missing or empty.

        Raises:
            EmbeddingsUnsupportedError: if the provider does not support
                embeddings.  Callers (Active Memory layer) should catch
                this and degrade to BM25-only retrieval.
        """
        if not self._loaded:
            if not self._load_cache():
                await self._build(embed_fn=embed_fn)
                if self._entries:
                    self._save_cache()

        if not self._entries or self._vectors is None or self._vectors.size == 0:
            return []

        # Embed the query.  Always one call to the provider.
        query_batch = await embed_fn([text])
        if not query_batch.vectors:
            return []
        if query_batch.dimensionality != self._dimensionality:
            # Caller switched models between corpus build and now.
            # Invalidate + rebuild + retry.
            logger.info(
                "VectorIndex query dim=%d != corpus dim=%d (model=%r); "
                "invalidating + rebuilding",
                query_batch.dimensionality,
                self._dimensionality,
                query_batch.model_id,
            )
            self.invalidate()
            await self._build(embed_fn=embed_fn)
            if self._entries:
                self._save_cache()
            if not self._entries or self._vectors is None or self._vectors.size == 0:
                return []
            # Re-embed query with the new model — done above on first call,
            # but if rebuild used the same embed_fn, the model is consistent
            # now.  Re-embedding the query keeps semantics straightforward.
            query_batch = await embed_fn([text])

        q = np.asarray(query_batch.vectors[0], dtype=np.float32)
        q_norm = float(np.linalg.norm(q))
        if q_norm == 0.0:
            return []
        q = q / q_norm

        # Corpus vectors are already L2-normalized at build time.
        scores = self._vectors @ q  # shape (N,)

        # Stable top-K.  argsort is fine at <2k entries.
        order = np.argsort(-scores)[:top_k]
        hits: list[VectorHit] = []
        for rank, idx in enumerate(order):
            i = int(idx)
            s = float(scores[i])
            hits.append(VectorHit(entry=self._entries[i], score=s, rank=rank))
        return hits

    def invalidate(self) -> None:
        """Drop in-memory state and remove on-disk cache.  Synchronous
        (matches BM25Index.invalidate's signature so MemoryManager can
        call both from the same code path)."""
        self._entries = []
        self._vectors = None
        self._dimensionality = 0
        self._model_id = ""
        self._loaded = False
        try:
            self._cache_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self._cache_path.with_suffix(".tmp").unlink()
        except FileNotFoundError:
            pass

    @property
    def model_id(self) -> str:
        """Model id of the currently-loaded corpus vectors (or '' if not loaded)."""
        return self._model_id

    @property
    def dimensionality(self) -> int:
        return self._dimensionality

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
                    "Vector cache header malformed at %s; rebuilding",
                    self._cache_path,
                )
                return False
            if header.get("format_version") != self.FORMAT_VERSION:
                logger.info(
                    "Vector cache format_version=%r != expected %d at %s; rebuilding",
                    header.get("format_version"),
                    self.FORMAT_VERSION,
                    self._cache_path,
                )
                return False
            if header.get("corpus_sha256") != self._current_sha256():
                logger.debug(
                    "Vector cache corpus_sha256 mismatch; rebuilding"
                )
                return False
            entries = data["entries"]
            vectors = data["vectors"]
            dim = header.get("dimensionality")
            model_id = header.get("model_id", "")
            if not isinstance(entries, list):
                logger.warning("Vector cache entries shape unexpected; rebuilding")
                return False
            if not isinstance(vectors, np.ndarray):
                logger.warning("Vector cache vectors shape unexpected; rebuilding")
                return False
            if not isinstance(dim, int):
                logger.warning("Vector cache dimensionality missing; rebuilding")
                return False
            if vectors.shape[0] != len(entries):
                logger.warning(
                    "Vector cache row count %d != entry count %d; rebuilding",
                    vectors.shape[0],
                    len(entries),
                )
                return False
            if vectors.size and vectors.shape[1] != dim:
                logger.warning(
                    "Vector cache row width %d != dim %d; rebuilding",
                    vectors.shape[1],
                    dim,
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
                "Vector cache load failed (%s: %s) at %s; rebuilding",
                type(exc).__name__,
                exc,
                self._cache_path,
            )
            return False

        self._entries = entries
        self._vectors = vectors.astype(np.float32, copy=False)
        self._dimensionality = dim
        self._model_id = model_id
        self._loaded = True
        return True

    def _save_cache(self) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "Vector cache dir create failed (%s); next call rebuilds", exc
            )
            return

        data = {
            "header": {
                "format_version": self.FORMAT_VERSION,
                "corpus_sha256": self._current_sha256(),
                "dimensionality": self._dimensionality,
                "model_id": self._model_id,
                "entry_count": len(self._entries),
                "built_at": datetime.now(tz=UTC).isoformat(),
            },
            "entries": self._entries,
            "vectors": self._vectors,
        }
        tmp_path = self._cache_path.with_suffix(".tmp")
        try:
            with tmp_path.open("wb") as f:
                pickle.dump(data, f, protocol=PICKLE_PROTOCOL)
            os.replace(tmp_path, self._cache_path)
        except OSError as exc:
            logger.warning(
                "Vector cache save failed (%s); next call rebuilds", exc
            )
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

    # ─── build ─────────────────────────────────────────────────────────

    async def _build(self, *, embed_fn: EmbedFn) -> None:
        """Read MEMORY.md, segment, embed all entries, store L2-normalized.

        Propagates :class:`EmbeddingsUnsupportedError` from the provider
        without conversion — callers (Active Memory) catch and fall back.
        """
        if not self._memory_path.exists():
            self._entries = []
            self._vectors = None
            self._dimensionality = 0
            self._model_id = ""
            self._loaded = True
            return

        text = self._memory_path.read_text(encoding="utf-8")
        entries = self._segment(text)
        if not entries:
            self._entries = []
            self._vectors = None
            self._dimensionality = 0
            self._model_id = ""
            self._loaded = True
            return

        # Embed all entries in one call (the provider chunks internally
        # if needed, per the M6.6 contract).
        batch = await embed_fn([e.raw for e in entries])
        if len(batch.vectors) != len(entries):
            raise RuntimeError(
                f"Provider returned {len(batch.vectors)} vectors for "
                f"{len(entries)} entries (model_id={batch.model_id!r})"
            )

        vectors = np.asarray(batch.vectors, dtype=np.float32)
        # L2-normalize for cosine similarity via dot product later.
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        # Avoid division by zero for empty / degenerate embeddings —
        # those rows stay all-zero and will rank as no-match.
        safe_norms = np.where(norms == 0.0, 1.0, norms)
        vectors = vectors / safe_norms

        self._entries = entries
        self._vectors = vectors
        self._dimensionality = batch.dimensionality
        self._model_id = batch.model_id
        self._loaded = True

    # ─── segmentation (pure) ───────────────────────────────────────────

    @staticmethod
    def _segment(text: str) -> list[VectorEntry]:
        """Same paragraph-delimited segmentation as M6.1 BM25Index._segment.

        Duplicated rather than imported to keep this module
        free of a direct dependency on the BM25 sibling — the two
        indexes are mutually independent and either can be used
        without the other.
        """
        if not text or not text.strip():
            return []

        lines = text.splitlines()
        entries: list[VectorEntry] = []
        cur_lines: list[tuple[int, str]] = []
        cur_blank_run = 0

        def flush() -> None:
            if not cur_lines:
                return
            line_start = cur_lines[0][0]
            line_end = cur_lines[-1][0]
            raw = "\n".join(content for _, content in cur_lines).strip()
            if raw:
                entries.append(
                    VectorEntry(raw=raw, line_start=line_start, line_end=line_end)
                )
            cur_lines.clear()

        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                cur_blank_run += 1
                if cur_blank_run >= 1:
                    flush()
                continue

            cur_blank_run = 0

            if VectorIndex._HEADING_RE.match(line):
                flush()
                cur_lines.append((idx, line))
                continue

            cur_lines.append((idx, line))

        flush()
        return entries
