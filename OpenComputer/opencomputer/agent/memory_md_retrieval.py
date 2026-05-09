"""Per-turn hybrid retrieval over ``MEMORY.md`` (v1.1 plan-3 M6.3).

Composes BM25 (M6.1) and vector (M6.2) retrieval into a single ranked
list via Reciprocal Rank Fusion (RRF), then renders an injection block
that the agent loop appends to the system prompt before each LLM call.

This is the **Plan 3 "Active Memory"** layer — distinct from the existing
:class:`opencomputer.agent.active_memory.ActiveMemoryInjector` (the
OpenClaw 1.B-alt port that retrieves from SessionDB FTS5).  Both layers
can be enabled simultaneously; their outputs compose in the per-turn
system prompt:

    [base system] + [injected mode] + [Honcho prefetch]
                  + [MEMORY.md retrieval]  ← THIS MODULE
                  + [SessionDB FTS5 active memory]   (legacy ``active_memory.py``)
                  + [channel context]
    + user_message  (user role)

Ordering rationale (carry-forward audit note from M6.1 brainstorm):
- Honcho prefetch first — its corpus is external + most variable, so
  putting it earliest concentrates cache invalidation in one place.
- MEMORY.md retrieval second — per-profile knowledge that changes only
  on explicit Memory tool writes; medium variability.
- SessionDB FTS5 active memory third — per-session episodic + message
  recall; highest variability per turn.

Architecture:
- ``MemoryMdRetriever`` accepts a :class:`MemoryManager` (which owns the
  BM25 + vector indexes) and an optional ``embed_fn``.  A None embed_fn
  (or one that raises :class:`EmbeddingsUnsupportedError`) cleanly
  degrades to BM25-only retrieval with a one-time WARNING log.
- :func:`reciprocal_rank_fusion` is implementation-detail-public so the
  RRF parameters can be tested independently of the index plumbing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from plugin_sdk.embeddings import EmbeddingBatch, EmbeddingsUnsupportedError

logger = logging.getLogger("opencomputer.agent.memory_md_retrieval")

# Type alias matching VectorIndex.query's embed_fn parameter.
EmbedFn = Callable[[list[str]], Awaitable[EmbeddingBatch]]

# Reciprocal Rank Fusion smoothing constant.  Convention is k=60
# (Cormack-Clarke-Buettcher 2009).  Lower k weighs the top result
# more heavily; we use the standard.
RRF_K: int = 60

# Default per-source recall before fusion.  Each index returns top-K
# candidates; the fused list is then truncated to the caller's top_k.
DEFAULT_PER_SOURCE_K: int = 20

# Default top-K returned to the caller after fusion.
DEFAULT_TOP_K: int = 5


@dataclass(frozen=True, slots=True)
class FusedHit:
    """A retrieval hit from one or both indexes, post-RRF.

    Fields:
        raw:           The entry's raw text (matches both BM25Index and
                       VectorIndex entry.raw exactly — same MEMORY.md
                       segmentation for both).
        line_start:    1-indexed first line of the entry in MEMORY.md.
        line_end:      1-indexed last line of the entry.
        rrf_score:     Sum of 1/(RRF_K + rank) across all sources where
                       this entry appeared.
        bm25_rank:     0-indexed rank in the BM25 source list, or None
                       if BM25 did not return this entry.
        vector_rank:   0-indexed rank in the vector source list, or
                       None if vector did not return / was unavailable.
        rank:          0-indexed final position in the fused list.
    """

    raw: str
    line_start: int
    line_end: int
    rrf_score: float
    bm25_rank: int | None
    vector_rank: int | None
    rank: int


def reciprocal_rank_fusion(
    *ranked_lists: list[tuple[str, int, int]],
    top_k: int = DEFAULT_TOP_K,
    k_constant: int = RRF_K,
) -> list[FusedHit]:
    """Combine multiple ranked lists via Reciprocal Rank Fusion.

    Each input list is a sequence of ``(raw_text, line_start, line_end)``
    tuples in rank order (rank 0 = best).  Entries with the same
    ``raw_text`` across lists are fused; their RRF scores sum.

    Returns the top_k fused entries by descending rrf_score, with ties
    broken deterministically by the lex order of the raw text.
    """
    score: dict[str, float] = {}
    meta: dict[str, tuple[int, int]] = {}  # raw → (line_start, line_end)
    per_source_rank: dict[str, list[int | None]] = {}

    n_sources = len(ranked_lists)
    for source_idx, ranked in enumerate(ranked_lists):
        for rank, (raw, ls, le) in enumerate(ranked):
            score[raw] = score.get(raw, 0.0) + 1.0 / (k_constant + rank)
            if raw not in meta:
                meta[raw] = (ls, le)
            ranks = per_source_rank.setdefault(raw, [None] * n_sources)
            # Keep the earliest rank per source if duplicated within a source
            if ranks[source_idx] is None or rank < ranks[source_idx]:
                ranks[source_idx] = rank

    ordered = sorted(
        score.items(),
        key=lambda kv: (-kv[1], kv[0]),  # higher score first; lex for tie-break
    )
    out: list[FusedHit] = []
    for final_rank, (raw, sc) in enumerate(ordered[:top_k]):
        ls, le = meta[raw]
        ranks = per_source_rank.get(raw, [None] * n_sources)
        bm25_rank = ranks[0] if n_sources >= 1 else None
        vec_rank = ranks[1] if n_sources >= 2 else None
        out.append(
            FusedHit(
                raw=raw,
                line_start=ls,
                line_end=le,
                rrf_score=sc,
                bm25_rank=bm25_rank,
                vector_rank=vec_rank,
                rank=final_rank,
            )
        )
    return out


class MemoryMdRetriever:
    """Per-turn hybrid retriever over ``MEMORY.md``.

    Usage from the agent loop:

        retriever = MemoryMdRetriever(memory_manager, embed_fn=provider.embed)
        hits = await retriever.retrieve(user_message)
        block = retriever.inject_block(hits)
        if block:
            system_prompt += "\\n\\n" + block

    On a provider that does not implement ``embed`` the call still works
    (vector retrieval is skipped, BM25 alone supplies the hits) and a
    one-time WARNING is logged.
    """

    INJECT_BLOCK_HEADER: str = "## Active memory (MEMORY.md retrieval)"

    def __init__(
        self,
        memory_manager,  # type: ignore[no-untyped-def]  # avoid circular import; duck-typed
        *,
        embed_fn: EmbedFn | None = None,
        per_source_k: int = DEFAULT_PER_SOURCE_K,
        top_k: int = DEFAULT_TOP_K,
        k_constant: int = RRF_K,
    ) -> None:
        self._mm = memory_manager
        self._embed_fn = embed_fn
        self._per_source_k = per_source_k
        self._top_k = top_k
        self._k_constant = k_constant
        self._embeddings_unsupported_logged = False

    async def retrieve(self, query: str) -> list[FusedHit]:
        """Retrieve top hits from BM25 + vector and fuse via RRF.

        Returns an empty list if both indexes are empty / no hits / no
        embed_fn.  Never raises (provider errors degrade gracefully —
        ``EmbeddingsUnsupportedError`` is caught and logged once;
        any other exception is logged and treated as "no vector hits").
        """
        if not query or not query.strip():
            return []

        bm25_ranked = self._collect_bm25(query)
        vec_ranked = await self._collect_vector(query)

        if not bm25_ranked and not vec_ranked:
            return []

        # Pass each list to the fuser even if empty — RRF handles it.
        return reciprocal_rank_fusion(
            bm25_ranked,
            vec_ranked,
            top_k=self._top_k,
            k_constant=self._k_constant,
        )

    def inject_block(self, hits: list[FusedHit]) -> str:
        """Render the system-prompt block for a list of fused hits.

        Returns an empty string when the hit list is empty so callers
        can safely concatenate the result without conditionals.
        """
        if not hits:
            return ""
        lines = [self.INJECT_BLOCK_HEADER, ""]
        for h in hits:
            # Compact two-line entry: rank-prefixed first line + indented body.
            line_no = (
                f"L{h.line_start}"
                if h.line_start == h.line_end
                else f"L{h.line_start}-{h.line_end}"
            )
            sources: list[str] = []
            if h.bm25_rank is not None:
                sources.append(f"bm25#{h.bm25_rank}")
            if h.vector_rank is not None:
                sources.append(f"vec#{h.vector_rank}")
            src = ",".join(sources) if sources else "?"
            lines.append(f"- [{line_no} via {src}] {h.raw}")
        return "\n".join(lines)

    # ─── internals ────────────────────────────────────────────────────

    def _collect_bm25(self, query: str) -> list[tuple[str, int, int]]:
        try:
            hits = self._mm.bm25_index.query(query, top_k=self._per_source_k)
        except Exception as exc:  # noqa: BLE001 — never crash the loop on a retrieval issue
            logger.warning("BM25 retrieval failed: %s", exc)
            return []
        return [(h.entry.raw, h.entry.line_start, h.entry.line_end) for h in hits]

    async def _collect_vector(self, query: str) -> list[tuple[str, int, int]]:
        if self._embed_fn is None:
            return []
        try:
            hits = await self._mm.vector_index.query(
                query, embed_fn=self._embed_fn, top_k=self._per_source_k
            )
        except EmbeddingsUnsupportedError:
            if not self._embeddings_unsupported_logged:
                logger.warning(
                    "Vector retrieval disabled — provider does not support "
                    "embeddings; falling back to BM25-only.  This message "
                    "logs once per retriever instance."
                )
                self._embeddings_unsupported_logged = True
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vector retrieval failed: %s", exc)
            return []
        return [(h.entry.raw, h.entry.line_start, h.entry.line_end) for h in hits]
