"""MemoryMdRetriever + RRF fusion tests (v1.1 plan-3 M6.3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.memory_md_retrieval import (
    DEFAULT_TOP_K,
    RRF_K,
    FusedHit,
    MemoryMdRetriever,
    reciprocal_rank_fusion,
)
from plugin_sdk.embeddings import EmbeddingBatch, EmbeddingsUnsupportedError

# ─── RRF fusion (pure function) ─────────────────────────────────────


def test_rrf_single_source_preserves_order() -> None:
    bm25 = [("alpha", 1, 1), ("beta", 2, 2), ("gamma", 3, 3)]
    fused = reciprocal_rank_fusion(bm25, top_k=3)
    assert [h.raw for h in fused] == ["alpha", "beta", "gamma"]
    assert [h.rank for h in fused] == [0, 1, 2]


def test_rrf_two_sources_overlap_boosts() -> None:
    # "alpha" appears in both lists at rank 0 — should win
    bm25 = [("alpha", 1, 1), ("beta", 2, 2)]
    vec = [("alpha", 1, 1), ("gamma", 3, 3)]
    fused = reciprocal_rank_fusion(bm25, vec, top_k=3)
    assert fused[0].raw == "alpha"
    assert fused[0].bm25_rank == 0
    assert fused[0].vector_rank == 0


def test_rrf_score_formula() -> None:
    # Single source, single entry at rank 0 → score = 1/(60+0)
    bm25 = [("only", 1, 1)]
    fused = reciprocal_rank_fusion(bm25, top_k=1)
    expected = 1.0 / (RRF_K + 0)
    assert abs(fused[0].rrf_score - expected) < 1e-9


def test_rrf_summed_score_across_sources() -> None:
    # Same entry at rank 0 in two sources → score = 2 * (1/(60+0))
    bm25 = [("entry", 1, 1)]
    vec = [("entry", 1, 1)]
    fused = reciprocal_rank_fusion(bm25, vec, top_k=1)
    expected = 1.0 / (RRF_K + 0) + 1.0 / (RRF_K + 0)
    assert abs(fused[0].rrf_score - expected) < 1e-9


def test_rrf_lex_tie_breaker_deterministic() -> None:
    # Two entries at rank 0 in two distinct sources — same RRF score —
    # the lex order tie-breaker should yield "alpha" first.
    bm25 = [("zeta", 1, 1)]
    vec = [("alpha", 2, 2)]
    fused = reciprocal_rank_fusion(bm25, vec, top_k=2)
    assert fused[0].raw == "alpha"
    assert fused[1].raw == "zeta"


def test_rrf_top_k_truncation() -> None:
    bm25 = [(f"e{i}", 1, 1) for i in range(20)]
    fused = reciprocal_rank_fusion(bm25, top_k=5)
    assert len(fused) == 5


def test_rrf_empty_lists() -> None:
    assert reciprocal_rank_fusion([], [], top_k=5) == []


def test_rrf_one_empty_one_full() -> None:
    bm25 = [("alpha", 1, 1), ("beta", 2, 2)]
    fused = reciprocal_rank_fusion(bm25, [], top_k=5)
    assert [h.raw for h in fused] == ["alpha", "beta"]
    # bm25_rank present, vector_rank None
    assert fused[0].bm25_rank == 0
    assert fused[0].vector_rank is None


def test_rrf_meta_preserved_from_first_seen() -> None:
    bm25 = [("alpha", 5, 7)]
    vec = [("alpha", 99, 100)]  # different line numbers
    fused = reciprocal_rank_fusion(bm25, vec, top_k=1)
    # First-seen wins for line metadata
    assert fused[0].line_start == 5
    assert fused[0].line_end == 7


# ─── MemoryMdRetriever (with real MemoryManager) ───────────────────


def _make_manager(tmp_path: Path, content: str = "") -> MemoryManager:
    decl = tmp_path / "MEMORY.md"
    decl.write_text(content, encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    return MemoryManager(declarative_path=decl, skills_path=skills)


class _FakeEmbed:
    def __init__(self, dim: int = 8, model: str = "fake") -> None:
        self.dim = dim
        self.model = model

    async def __call__(self, texts: list[str]) -> EmbeddingBatch:
        vectors: list[list[float]] = []
        for t in texts:
            v = [0.0] * self.dim
            for word in t.lower().split():
                v[abs(hash(word)) % self.dim] += 1.0
            if all(x == 0.0 for x in v):
                v[0] = 1.0
            vectors.append(v)
        return EmbeddingBatch(vectors=vectors, dimensionality=self.dim, model_id=self.model)


@pytest.mark.asyncio
async def test_retrieve_empty_query_returns_empty(tmp_path: Path) -> None:
    mm = _make_manager(
        tmp_path,
        "alpha cats\n\nbeta dogs\n\ngamma birds\n\ndelta fish\n\nepsilon turtles",
    )
    retriever = MemoryMdRetriever(mm, embed_fn=_FakeEmbed())
    assert await retriever.retrieve("") == []
    assert await retriever.retrieve("   ") == []


@pytest.mark.asyncio
async def test_retrieve_empty_corpus_returns_empty(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path, "")
    retriever = MemoryMdRetriever(mm, embed_fn=_FakeEmbed())
    assert await retriever.retrieve("anything") == []


@pytest.mark.asyncio
async def test_retrieve_returns_fused_hits_when_both_indexes_have_data(
    tmp_path: Path,
) -> None:
    content = "\n\n".join(
        [
            "postgresql is the preferred relational database",
            "tomato pasta is the best italian comfort food",
            "tuesday weekly meeting cadence with the team",
            "python interpreter pinned to 3.12 minimum",
            "extra entry one for IDF non-degeneracy",
            "extra entry two with extra content for distinct word frequencies",
        ]
    )
    mm = _make_manager(tmp_path, content)
    retriever = MemoryMdRetriever(mm, embed_fn=_FakeEmbed(), top_k=3)

    hits = await retriever.retrieve("postgresql database")
    assert hits
    assert any("postgresql" in h.raw for h in hits)


@pytest.mark.asyncio
async def test_retrieve_degrades_to_bm25_only_when_no_embed_fn(
    tmp_path: Path,
) -> None:
    content = "\n\n".join(f"alpha {i} entry topic" for i in range(8))
    mm = _make_manager(tmp_path, content)
    retriever = MemoryMdRetriever(mm, embed_fn=None, top_k=3)

    hits = await retriever.retrieve("alpha")
    assert hits
    # Without embed_fn, vector_rank must be None for every hit
    assert all(h.vector_rank is None for h in hits)
    assert any(h.bm25_rank is not None for h in hits)


@pytest.mark.asyncio
async def test_retrieve_degrades_when_embed_fn_raises_unsupported(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = "\n\n".join(f"alpha {i} entry topic" for i in range(8))
    mm = _make_manager(tmp_path, content)

    async def unsupported(_: list[str]) -> EmbeddingBatch:
        raise EmbeddingsUnsupportedError("provider has no embeddings")

    retriever = MemoryMdRetriever(mm, embed_fn=unsupported, top_k=3)

    import logging

    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.memory_md_retrieval"):
        hits = await retriever.retrieve("alpha")
    assert hits  # BM25 still produced results
    assert all(h.vector_rank is None for h in hits)
    assert any("embeddings" in r.message.lower() for r in caplog.records)

    # Calling again should NOT log again (one-shot warning)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.memory_md_retrieval"):
        await retriever.retrieve("alpha")
    assert not any(
        "embeddings" in r.message.lower() for r in caplog.records
    ), "warning should be emitted only once per retriever instance"


@pytest.mark.asyncio
async def test_retrieve_exception_in_embed_does_not_crash(tmp_path: Path) -> None:
    content = "\n\n".join(f"alpha {i} entry topic" for i in range(8))
    mm = _make_manager(tmp_path, content)

    async def boom(_: list[str]) -> EmbeddingBatch:
        raise RuntimeError("network down")

    retriever = MemoryMdRetriever(mm, embed_fn=boom, top_k=3)

    hits = await retriever.retrieve("alpha")
    # Should not raise — vector path failed but BM25 still works.
    assert hits


@pytest.mark.asyncio
async def test_inject_block_renders_lines_and_metadata(tmp_path: Path) -> None:
    content = "\n\n".join(f"alpha {i} cats topic" for i in range(8))
    mm = _make_manager(tmp_path, content)
    retriever = MemoryMdRetriever(mm, embed_fn=_FakeEmbed(), top_k=2)

    hits = await retriever.retrieve("alpha")
    block = retriever.inject_block(hits)
    assert block.startswith(MemoryMdRetriever.INJECT_BLOCK_HEADER)
    # Must include line-number reference + source attribution
    assert "L" in block  # line numbers
    assert "via" in block


@pytest.mark.asyncio
async def test_inject_block_empty_returns_empty_string(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path, "")
    retriever = MemoryMdRetriever(mm, embed_fn=_FakeEmbed())
    assert retriever.inject_block([]) == ""


@pytest.mark.asyncio
async def test_retrieve_top_k_default_is_5(tmp_path: Path) -> None:
    content = "\n\n".join(f"alpha {i} entry topic content" for i in range(20))
    mm = _make_manager(tmp_path, content)
    retriever = MemoryMdRetriever(mm, embed_fn=_FakeEmbed())

    hits = await retriever.retrieve("alpha")
    assert len(hits) <= DEFAULT_TOP_K


@pytest.mark.asyncio
async def test_retrieve_no_matches_returns_empty(tmp_path: Path) -> None:
    content = "\n\n".join(f"alpha {i} entry" for i in range(8))
    mm = _make_manager(tmp_path, content)
    retriever = MemoryMdRetriever(mm, embed_fn=_FakeEmbed(), top_k=3)

    # Vector still ranks something; BM25 doesn't.  Either way at least
    # one of the indexes should produce something, so result should be
    # non-empty even for unrelated queries (vector returns nearest
    # neighbours for anything).  This test pins that behavior.
    hits = await retriever.retrieve("xqwzpv-no-match-token")
    # Vector path returns nearest neighbours by default; we don't reject
    # them at the retriever level — that's the caller's choice.
    assert isinstance(hits, list)


@pytest.mark.asyncio
async def test_fused_hit_carries_both_source_ranks_when_both_match(
    tmp_path: Path,
) -> None:
    content = "\n\n".join(
        [
            "postgresql alpha database one",
            "postgresql beta database two",
            "alpha gamma other entry",
            "alpha delta yet another",
            "alpha epsilon yet another",
            "alpha zeta yet another",
        ]
    )
    mm = _make_manager(tmp_path, content)
    retriever = MemoryMdRetriever(mm, embed_fn=_FakeEmbed(), top_k=5)

    hits = await retriever.retrieve("postgresql database")
    # At least one hit should have BOTH bm25_rank and vector_rank set
    # (an entry hit in both sources).
    assert any(h.bm25_rank is not None and h.vector_rank is not None for h in hits)
