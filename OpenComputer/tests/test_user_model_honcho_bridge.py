"""Tests for F4HonchoBridge (Phase 4.B of catch-up plan).

Real Honcho integration is deferred (needs a running Postgres + Deriver
in CI). These tests use a minimal in-memory fake that satisfies
``HonchoLike`` so we can exercise the cycle-prevention + materialisation
logic end-to-end.
"""

from __future__ import annotations

import pytest

from opencomputer.user_model.honcho_bridge import (
    F4HonchoBridge,
    HonchoSynthesis,
)
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.user_model import Edge

# ---------- Fake Honcho ----------


class _FakeHoncho:
    """Records observed edges + replays a queued synthesis."""

    def __init__(self, synthesis: HonchoSynthesis | None = None):
        self.observed: list[Edge] = []
        self.dialectic_queries: list[str] = []
        self._next_synthesis = synthesis
        self.observe_should_fail = False
        self.dialectic_should_fail = False

    async def observe_edge(self, edge: Edge) -> None:
        if self.observe_should_fail:
            raise RuntimeError("simulated honcho observe failure")
        self.observed.append(edge)

    async def dialectic(self, query: str) -> HonchoSynthesis | None:
        if self.dialectic_should_fail:
            raise RuntimeError("simulated honcho dialectic failure")
        self.dialectic_queries.append(query)
        return self._next_synthesis


# ---------- Fixtures ----------


@pytest.fixture
def store(tmp_path):
    return UserModelStore(tmp_path / "user_model.sqlite")


def _seed_user_node(store: UserModelStore) -> str:
    n = store.upsert_node(kind="identity", value="user", confidence=1.0)
    return n.node_id


def _seed_motif_edge(store: UserModelStore, source: str = "motif_importer") -> Edge:
    a = store.upsert_node(kind="attribute", value=f"a-{source}", confidence=0.8)
    b = store.upsert_node(kind="attribute", value=f"b-{source}", confidence=0.8)
    e = Edge(
        kind="asserts", from_node=a.node_id, to_node=b.node_id,
        salience=0.7, confidence=0.7, source=source,
    )
    store.insert_edge(e)
    return e


# ---------- F4 → Honcho one-way feed ----------


@pytest.mark.asyncio
async def test_feed_recent_pushes_motif_edges(store):
    _seed_motif_edge(store)
    _seed_motif_edge(store)
    honcho = _FakeHoncho()
    bridge = F4HonchoBridge(store=store, honcho=honcho)
    n = await bridge.feed_recent()
    assert n == 2
    assert len(honcho.observed) == 2


@pytest.mark.asyncio
async def test_feed_recent_skips_honcho_synthesis_edges(store):
    """Cycle prevention — honcho-tagged edges must NOT be re-fed."""
    _seed_motif_edge(store, source="motif_importer")
    _seed_motif_edge(store, source="honcho_synthesis")
    _seed_motif_edge(store, source="honcho_dialectic")
    honcho = _FakeHoncho()
    bridge = F4HonchoBridge(store=store, honcho=honcho)
    n = await bridge.feed_recent()
    assert n == 1  # only the motif_importer edge fed
    assert all(not e.source.startswith("honcho_") for e in honcho.observed)


@pytest.mark.asyncio
async def test_feed_recent_is_idempotent(store):
    """Same edge isn't pushed twice across calls."""
    _seed_motif_edge(store)
    honcho = _FakeHoncho()
    bridge = F4HonchoBridge(store=store, honcho=honcho)
    first = await bridge.feed_recent()
    second = await bridge.feed_recent()
    assert first == 1
    assert second == 0
    assert len(honcho.observed) == 1


@pytest.mark.asyncio
async def test_feed_recent_swallows_observe_failures(store):
    _seed_motif_edge(store)
    _seed_motif_edge(store)
    honcho = _FakeHoncho()
    honcho.observe_should_fail = True
    bridge = F4HonchoBridge(store=store, honcho=honcho)
    n = await bridge.feed_recent()
    # n still reports the count of *attempted* feeds; failures logged.
    assert n == 2


@pytest.mark.asyncio
async def test_feed_recent_respects_feed_limit(store):
    for _ in range(20):
        _seed_motif_edge(store)
    honcho = _FakeHoncho()
    bridge = F4HonchoBridge(store=store, honcho=honcho, feed_limit=5)
    n = await bridge.feed_recent()
    assert n == 5
    assert len(honcho.observed) == 5


# ---------- Honcho synthesis → F4 materialisation ----------


@pytest.mark.asyncio
async def test_synthesize_materializes_low_confidence_edge(store):
    user_id = _seed_user_node(store)
    honcho = _FakeHoncho(synthesis=HonchoSynthesis(
        claim="user prefers concise responses",
        confidence=0.8,
    ))
    bridge = F4HonchoBridge(store=store, honcho=honcho)
    edge = await bridge.synthesize_and_materialize(
        "what kind of responses?", anchor_node_id=user_id,
    )
    assert edge is not None
    assert edge.source == "honcho_synthesis"
    # 0.8 input × 0.5 confidence_scale = 0.4
    assert edge.confidence == pytest.approx(0.4)
    assert edge.from_node == user_id
    # The claim should have a node now
    fetched_to = store.get_node(edge.to_node)
    assert fetched_to is not None
    assert "concise" in fetched_to.value


@pytest.mark.asyncio
async def test_synthesize_below_threshold_returns_none(store):
    user_id = _seed_user_node(store)
    honcho = _FakeHoncho(synthesis=HonchoSynthesis(
        claim="weak signal", confidence=0.2,
    ))
    bridge = F4HonchoBridge(store=store, honcho=honcho)
    edge = await bridge.synthesize_and_materialize(
        "anything?", anchor_node_id=user_id,
    )
    assert edge is None


@pytest.mark.asyncio
async def test_synthesize_no_answer_returns_none(store):
    user_id = _seed_user_node(store)
    honcho = _FakeHoncho(synthesis=None)
    bridge = F4HonchoBridge(store=store, honcho=honcho)
    edge = await bridge.synthesize_and_materialize(
        "anything?", anchor_node_id=user_id,
    )
    assert edge is None


@pytest.mark.asyncio
async def test_synthesize_swallows_dialectic_failure(store):
    user_id = _seed_user_node(store)
    honcho = _FakeHoncho(synthesis=HonchoSynthesis(claim="x", confidence=0.9))
    honcho.dialectic_should_fail = True
    bridge = F4HonchoBridge(store=store, honcho=honcho)
    edge = await bridge.synthesize_and_materialize(
        "any?", anchor_node_id=user_id,
    )
    assert edge is None


@pytest.mark.asyncio
async def test_synthesized_edge_does_not_feed_back(store):
    """End-to-end cycle prevention check.

    Step 1: motif edge fed to honcho.
    Step 2: honcho synthesis materialised as honcho_synthesis edge.
    Step 3: re-feed — synthesis edge must NOT be fed back."""
    user_id = _seed_user_node(store)
    _seed_motif_edge(store)
    honcho = _FakeHoncho(synthesis=HonchoSynthesis(
        claim="user is a Python person", confidence=0.7,
    ))
    bridge = F4HonchoBridge(store=store, honcho=honcho)

    # Step 1
    await bridge.feed_recent()
    initial_observed = len(honcho.observed)

    # Step 2
    edge = await bridge.synthesize_and_materialize(
        "language preference?", anchor_node_id=user_id,
    )
    assert edge is not None

    # Step 3 — the new honcho_synthesis edge must NOT count as a new feed
    n2 = await bridge.feed_recent()
    assert n2 == 0
    assert len(honcho.observed) == initial_observed
