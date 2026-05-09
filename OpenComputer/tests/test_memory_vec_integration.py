"""MemoryManager owns one VectorIndex per profile and invalidates on writes (M6.2)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from opencomputer.agent.memory import MemoryManager
from plugin_sdk.embeddings import EmbeddingBatch


class _FakeEmbed:
    def __init__(self, dim: int = 4, model: str = "fake-model") -> None:
        self.dim = dim
        self.model = model
        self.calls: list[list[str]] = []

    async def __call__(self, texts: list[str]) -> EmbeddingBatch:
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for t in texts:
            v = [0.0] * self.dim
            for word in t.lower().split():
                idx = abs(hash(word)) % self.dim
                v[idx] += 1.0
            if all(x == 0.0 for x in v):
                v[0] = 1.0
            vectors.append(v)
        return EmbeddingBatch(
            vectors=vectors,
            dimensionality=self.dim,
            model_id=self.model,
        )


def _make_manager(tmp_path: Path) -> MemoryManager:
    decl = tmp_path / "MEMORY.md"
    decl.write_text("", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    return MemoryManager(declarative_path=decl, skills_path=skills)


def test_memory_manager_exposes_vector_index(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path)
    assert mm.vector_index is not None
    assert hasattr(mm.vector_index, "query")


@pytest.mark.asyncio
async def test_append_declarative_invalidates_vector_index(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path)
    embed = _FakeEmbed()

    mm.append_declarative("alpha entry one")
    mm.append_declarative("beta entry two")
    mm.append_declarative("gamma entry three")
    mm.append_declarative("delta entry four")
    mm.append_declarative("epsilon entry five")

    hits = await mm.vector_index.query("alpha", embed_fn=embed)
    assert hits  # at least one match


@pytest.mark.asyncio
async def test_replace_declarative_invalidates(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path)
    embed = _FakeEmbed()

    mm.append_declarative("alpha entry one")
    mm.append_declarative("beta entry two")
    mm.append_declarative("gamma entry three")
    mm.append_declarative("delta entry four")
    mm.append_declarative("epsilon entry five")

    # Trigger initial build
    await mm.vector_index.query("alpha", embed_fn=embed)
    initial_calls = len(embed.calls)

    mm.replace_declarative("epsilon", "rocketship")

    # New query should rebuild because invalidate() dropped the cache
    hits = await mm.vector_index.query("rocketship", embed_fn=embed)
    # Build call + query call should have happened on the post-invalidate path
    assert len(embed.calls) > initial_calls
    assert any("rocketship" in h.entry.raw for h in hits)


@pytest.mark.asyncio
async def test_remove_declarative_invalidates(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path)
    embed = _FakeEmbed()

    mm.append_declarative("alpha entry one")
    mm.append_declarative("beta entry two")
    mm.append_declarative("gamma entry three")
    mm.append_declarative("delta entry four")
    mm.append_declarative("epsilon entry five")

    await mm.vector_index.query("alpha", embed_fn=embed)

    mm.remove_declarative("beta entry two")

    hits = await mm.vector_index.query("alpha", embed_fn=embed)
    assert not any("beta" in h.entry.raw for h in hits)


@pytest.mark.asyncio
async def test_rebind_to_profile_swaps_vector_index(tmp_path: Path) -> None:
    profile_a = tmp_path / "a"
    profile_a.mkdir()
    decl_a = profile_a / "MEMORY.md"
    decl_a.write_text(
        "alpha cats\n\nbeta dogs\n\ngamma birds\n\ndelta fish\n\nepsilon turtles",
        encoding="utf-8",
    )
    skills_a = profile_a / "skills"
    skills_a.mkdir()

    profile_b = tmp_path / "b"
    profile_b.mkdir()
    decl_b = profile_b / "MEMORY.md"
    decl_b.write_text(
        "uno orbit\n\ndos rocketship\n\ntres satellite\n\ncuatro mission\n\ncinco probe",
        encoding="utf-8",
    )

    mm = MemoryManager(declarative_path=decl_a, skills_path=skills_a)
    embed = _FakeEmbed()
    a_hits = await mm.vector_index.query("alpha", embed_fn=embed)
    assert any("alpha" in h.entry.raw for h in a_hits)

    mm.rebind_to_profile(profile_b)
    b_hits = await mm.vector_index.query("rocketship", embed_fn=embed)
    assert any("rocketship" in h.entry.raw for h in b_hits)
    a_hits_after = await mm.vector_index.query("alpha", embed_fn=embed)
    assert not any("alpha" in h.entry.raw for h in a_hits_after)


@pytest.mark.asyncio
async def test_cache_lives_under_profile_home(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path)
    embed = _FakeEmbed()

    mm.append_declarative("alpha entry one")
    mm.append_declarative("beta entry two")
    mm.append_declarative("gamma entry three")
    mm.append_declarative("delta entry four")
    mm.append_declarative("epsilon entry five")

    await mm.vector_index.query("alpha", embed_fn=embed)
    assert (tmp_path / "cache" / "memory_vec.idx").exists()
