"""VectorIndex over MEMORY.md (v1.1 plan-3 M6.2)."""

from __future__ import annotations

import math
import pickle
from collections.abc import Callable, Iterable
from pathlib import Path

import numpy as np
import pytest

from opencomputer.agent.memory_vec_index import (
    VectorEntry,
    VectorHit,
    VectorIndex,
)
from plugin_sdk.embeddings import EmbeddingBatch, EmbeddingsUnsupportedError

# ─── deterministic fake embed_fn ────────────────────────────────────


class FakeEmbedder:
    """Deterministic embedding function for tests.

    Maps a text to a fixed-dim vector by summing per-word constants from
    a closed dictionary; unknown words contribute via a stable hash.  No
    randomness, no network — perfect for ranking assertions.
    """

    def __init__(
        self,
        dim: int = 8,
        model_id: str = "fake-test-embed-v1",
        keyword_weights: dict[str, list[float]] | None = None,
        record: list[list[str]] | None = None,
    ) -> None:
        self.dim = dim
        self.model_id = model_id
        self.keyword_weights = keyword_weights or {}
        self.record = record  # if provided, append each call's input

    async def __call__(self, texts: list[str]) -> EmbeddingBatch:
        if self.record is not None:
            self.record.append(list(texts))
        vectors: list[list[float]] = []
        for t in texts:
            v = self._embed_one(t)
            vectors.append(v)
        return EmbeddingBatch(
            vectors=vectors,
            dimensionality=self.dim,
            model_id=self.model_id,
            cost_estimate_usd=0.0,
            prompt_tokens=sum(len(t.split()) for t in texts),
        )

    def _embed_one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for word in text.lower().split():
            if word in self.keyword_weights:
                w = self.keyword_weights[word]
                for i in range(self.dim):
                    v[i] += w[i]
            else:
                # Stable hash → small contribution so unrelated words don't dominate.
                h = abs(hash(word)) % self.dim
                v[h] += 0.01
        # Avoid all-zero (cosine undefined).
        if all(x == 0.0 for x in v):
            v[0] = 1.0
        return v


def _write_memory(profile_home: Path, content: str) -> None:
    profile_home.mkdir(parents=True, exist_ok=True)
    (profile_home / "MEMORY.md").write_text(content, encoding="utf-8")


# ─── basic semantics ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_returns_empty_when_memory_md_missing(tmp_path: Path) -> None:
    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder()
    hits = await idx.query("anything", embed_fn=embed)
    assert hits == []


@pytest.mark.asyncio
async def test_query_returns_empty_when_memory_md_empty(tmp_path: Path) -> None:
    _write_memory(tmp_path, "")
    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder()
    hits = await idx.query("anything", embed_fn=embed)
    assert hits == []


@pytest.mark.asyncio
async def test_query_ranks_semantically_closer_entry_first(tmp_path: Path) -> None:
    weights = {
        "postgres": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "database": [0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "pasta": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "tomato": [0.0, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "tuesday": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "meeting": [0.0, 0.0, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0],
        "python": [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
        "interpreter": [0.0, 0.0, 0.0, 0.9, 0.0, 0.0, 0.0, 0.0],
    }
    content = "\n\n".join([
        "User prefers postgres for database workloads.",
        "Notes on cooking pasta with tomato sauce.",
        "Meeting cadence is weekly on tuesday.",
        "Python is the project's pinned interpreter.",
    ])
    _write_memory(tmp_path, content)
    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder(keyword_weights=weights)

    hits = await idx.query("postgres database", embed_fn=embed, top_k=2)
    assert len(hits) >= 1
    assert hits[0].rank == 0
    assert "postgres" in hits[0].entry.raw.lower()


@pytest.mark.asyncio
async def test_query_top_k_default_is_5(tmp_path: Path) -> None:
    content = "\n\n".join(f"entry {i}" for i in range(12))
    _write_memory(tmp_path, content)
    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder()

    hits = await idx.query("entry 0", embed_fn=embed)
    assert len(hits) <= 5


@pytest.mark.asyncio
async def test_query_score_in_cosine_range(tmp_path: Path) -> None:
    content = "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon"
    _write_memory(tmp_path, content)
    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder()

    hits = await idx.query("alpha", embed_fn=embed)
    for h in hits:
        assert -1.0 - 1e-6 <= h.score <= 1.0 + 1e-6


# ─── persistence + integrity ───────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_written_on_first_query(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma")
    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder()
    await idx.query("alpha", embed_fn=embed)
    assert (tmp_path / "cache" / "memory_vec.idx").exists()


@pytest.mark.asyncio
async def test_cache_loaded_on_second_instance_no_rebuild(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma")
    idx1 = VectorIndex(tmp_path)
    record_a: list[list[str]] = []
    embed1 = FakeEmbedder(record=record_a)
    await idx1.query("alpha", embed_fn=embed1)
    # First call: 1 build call (embed all entries) + 1 query call.
    assert len(record_a) == 2  # build + query

    # Fresh instance — should load cache; build path embeds nothing,
    # only the query call goes to embed.
    record_b: list[list[str]] = []
    embed2 = FakeEmbedder(record=record_b)
    idx2 = VectorIndex(tmp_path)
    await idx2.query("beta", embed_fn=embed2)
    assert len(record_b) == 1  # query only — corpus came from cache


@pytest.mark.asyncio
async def test_external_edit_triggers_rebuild(tmp_path: Path) -> None:
    mem = tmp_path / "MEMORY.md"
    mem.write_text("alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon", encoding="utf-8")

    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder()
    hits = await idx.query("alpha", embed_fn=embed)
    assert hits

    # External edit
    mem.write_text(
        "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon\n\nrocketship orbit",
        encoding="utf-8",
    )

    idx2 = VectorIndex(tmp_path)
    hits = await idx2.query("rocketship", embed_fn=embed)
    assert hits, "external edit must trigger rebuild"
    assert any("rocketship" in h.entry.raw for h in hits)


@pytest.mark.asyncio
async def test_truncated_cache_rebuilds_silently(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon")
    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder()
    await idx.query("alpha", embed_fn=embed)

    cache_path = tmp_path / "cache" / "memory_vec.idx"
    raw = cache_path.read_bytes()
    cache_path.write_bytes(raw[: len(raw) // 2])

    idx2 = VectorIndex(tmp_path)
    hits = await idx2.query("beta", embed_fn=embed)
    assert hits


@pytest.mark.asyncio
async def test_garbage_cache_rebuilds_silently(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "memory_vec.idx").write_bytes(b"this is not a pickle")

    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder()
    hits = await idx.query("alpha", embed_fn=embed)
    assert hits


@pytest.mark.asyncio
async def test_format_version_skew_rebuilds(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon")
    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder()
    await idx.query("alpha", embed_fn=embed)

    cache_path = tmp_path / "cache" / "memory_vec.idx"
    with cache_path.open("rb") as f:
        data = pickle.load(f)
    data["header"]["format_version"] = 999
    with cache_path.open("wb") as f:
        pickle.dump(data, f)

    idx2 = VectorIndex(tmp_path)
    hits = await idx2.query("alpha", embed_fn=embed)
    assert hits

    with cache_path.open("rb") as f:
        rebuilt = pickle.load(f)
    assert rebuilt["header"]["format_version"] == VectorIndex.FORMAT_VERSION


# ─── invalidation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalidate_removes_cache(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon")
    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder()
    await idx.query("alpha", embed_fn=embed)
    cache_path = tmp_path / "cache" / "memory_vec.idx"
    assert cache_path.exists()

    idx.invalidate()
    assert not cache_path.exists()


@pytest.mark.asyncio
async def test_invalidate_drops_in_memory_state(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon")
    idx = VectorIndex(tmp_path)
    embed = FakeEmbedder()
    await idx.query("alpha", embed_fn=embed)
    assert idx._entries

    idx.invalidate()
    assert not idx._entries
    assert idx._vectors is None


@pytest.mark.asyncio
async def test_invalidate_when_no_cache_exists_is_noop(tmp_path: Path) -> None:
    idx = VectorIndex(tmp_path)
    idx.invalidate()
    assert idx._entries == []


# ─── graceful degradation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_embeddings_unsupported_propagates(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon")
    idx = VectorIndex(tmp_path)

    async def unsupported(_: list[str]) -> EmbeddingBatch:
        raise EmbeddingsUnsupportedError("provider has no embeddings")

    with pytest.raises(EmbeddingsUnsupportedError, match="no embeddings"):
        await idx.query("alpha", embed_fn=unsupported)


# ─── model swap ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_swap_triggers_rebuild_via_dim_mismatch(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon")
    idx = VectorIndex(tmp_path)
    embed8 = FakeEmbedder(dim=8, model_id="dim-8-model")
    await idx.query("alpha", embed_fn=embed8)
    assert idx.dimensionality == 8

    # Swap embedder to a different dimensionality (simulates model swap)
    embed16 = FakeEmbedder(dim=16, model_id="dim-16-model")
    hits = await idx.query("beta", embed_fn=embed16)
    # After dim mismatch, the index should have rebuilt + re-indexed at
    # the new dim.
    assert idx.dimensionality == 16
    assert idx.model_id == "dim-16-model"


# ─── builds and shape checks ───────────────────────────────────────


@pytest.mark.asyncio
async def test_build_rejects_provider_count_mismatch(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon")
    idx = VectorIndex(tmp_path)

    async def bad(texts: list[str]) -> EmbeddingBatch:
        # Returns the wrong number of vectors
        return EmbeddingBatch(
            vectors=[[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8] for _ in texts[:-1]],
            dimensionality=8,
            model_id="bad-model",
        )

    with pytest.raises(RuntimeError, match="returned"):
        await idx.query("alpha", embed_fn=bad)


@pytest.mark.asyncio
async def test_zero_norm_query_returns_empty(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon")
    idx = VectorIndex(tmp_path)

    call_count = {"n": 0}

    async def embed(texts: list[str]) -> EmbeddingBatch:
        call_count["n"] += 1
        # Build call (n=1) — return real vectors so corpus is built.
        # Query call (n=2) — return all zero so the test asserts the
        # zero-norm guard fires on the query path.
        if call_count["n"] == 1:
            vecs = [[float(i) + 1, 0, 0, 0] for i in range(len(texts))]
        else:
            vecs = [[0.0, 0.0, 0.0, 0.0] for _ in texts]
        return EmbeddingBatch(vectors=vecs, dimensionality=4, model_id="zero-test")

    hits = await idx.query("anything", embed_fn=embed)
    assert hits == []


@pytest.mark.asyncio
async def test_l2_normalization_applied_to_corpus(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon")
    idx = VectorIndex(tmp_path)

    async def embed(texts: list[str]) -> EmbeddingBatch:
        # Build returns vectors of norm 5
        vecs = [[5.0, 0.0, 0.0, 0.0] for _ in texts]
        return EmbeddingBatch(vectors=vecs, dimensionality=4, model_id="norm-test")

    await idx.query("alpha", embed_fn=embed)

    # After build, corpus vectors should be L2-normalized (norm 1)
    norms = np.linalg.norm(idx._vectors, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


# ─── content-id + entry hashing ────────────────────────────────────


def test_vector_entry_content_id_is_stable() -> None:
    e1 = VectorEntry(raw="hello world", line_start=1, line_end=1)
    e2 = VectorEntry(raw="hello world", line_start=99, line_end=100)
    assert e1.content_id == e2.content_id


def test_vector_entry_content_id_differs_for_different_content() -> None:
    e1 = VectorEntry(raw="alpha", line_start=1, line_end=1)
    e2 = VectorEntry(raw="beta", line_start=1, line_end=1)
    assert e1.content_id != e2.content_id


# ─── segmentation parity with BM25 sibling ─────────────────────────


def test_segment_blank_line_separated() -> None:
    entries = VectorIndex._segment("first\n\nsecond")
    assert len(entries) == 2
    assert entries[0].raw == "first"
    assert entries[1].raw == "second"


def test_segment_heading_creates_boundary() -> None:
    entries = VectorIndex._segment("intro\n## Section\nbody")
    raws = [e.raw for e in entries]
    assert "intro" in raws
    assert any(r.startswith("## Section") for r in raws)


def test_segment_empty_returns_empty() -> None:
    assert VectorIndex._segment("") == []
    assert VectorIndex._segment("\n\n\n") == []
