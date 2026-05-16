"""M4 T4.4 — tests for the daily-gated user-model decay tick.

DecayDriftScheduler is never instantiated in the running agent, so
without this cron tick edge ``recency_weight`` would never be
recomputed. The tick runs the full decay pass at most once per day.
"""
from __future__ import annotations

import time
from pathlib import Path

from opencomputer.cron.system_jobs import _run_decay_tick
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.user_model import Edge, Node


def _seed_graph(tmp_path: Path, monkeypatch, *, edge_age_days: float) -> UserModelStore:
    """Profile-rooted store with one node pair + one aged edge."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    store = UserModelStore()
    store.insert_node(Node(node_id="a", kind="attribute", value="a"))
    store.insert_node(Node(node_id="b", kind="preference", value="b"))
    store.insert_edge(Edge(
        edge_id="e1", kind="asserts", from_node="a", to_node="b",
        recency_weight=1.0,
        created_at=time.time() - edge_age_days * 86400.0,
    ))
    return store


def test_decay_tick_runs_first_time(tmp_path, monkeypatch):
    """With no prior stamp the tick runs and reports edges updated."""
    _seed_graph(tmp_path, monkeypatch, edge_age_days=10.0)
    updated = _run_decay_tick()
    assert updated == 1
    # The stamp file is written so the next tick is gated.
    assert (tmp_path / "user_model" / ".last_decay_tick").exists()


def test_decay_tick_is_gated_within_24h(tmp_path, monkeypatch):
    """A second tick inside the 24h window is a no-op."""
    _seed_graph(tmp_path, monkeypatch, edge_age_days=10.0)
    assert _run_decay_tick() == 1
    assert _run_decay_tick() == 0  # gated — too soon


def test_decay_tick_ages_edge_recency_weight(tmp_path, monkeypatch):
    """An old edge's recency_weight is pulled below its stored 1.0."""
    store = _seed_graph(tmp_path, monkeypatch, edge_age_days=3650.0)
    _run_decay_tick()
    edge = store.get_edge("e1")
    assert edge is not None
    assert edge.recency_weight < 1.0


def test_decay_then_reranker_demotes_the_stale_fact(tmp_path, monkeypatch):
    """End-to-end: decay ages an edge → node_recency_score drops → the
    reranker demotes that fact."""
    from opencomputer.user_model.reranker import (
        RerankWeights,
        SessionContext,
        UserFactsReranker,
    )

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    store = UserModelStore()
    # Fresh fact — recent edge.
    store.insert_node(Node(node_id="fresh", kind="attribute", value="fresh"))
    store.insert_node(Node(node_id="ft", kind="preference", value="ft"))
    store.insert_edge(Edge(edge_id="ef", kind="asserts", from_node="fresh",
                           to_node="ft", recency_weight=1.0,
                           created_at=time.time()))
    # Stale fact — decade-old edge.
    store.insert_node(Node(node_id="stale", kind="attribute", value="stale"))
    store.insert_node(Node(node_id="st", kind="preference", value="st"))
    store.insert_edge(Edge(edge_id="es", kind="asserts", from_node="stale",
                           to_node="st", recency_weight=1.0,
                           created_at=time.time() - 3650 * 86400.0))

    _run_decay_tick()

    nodes = [store.get_node("fresh"), store.get_node("stale")]
    nodes = [n for n in nodes if n is not None]
    recency = {
        n.node_id: rs
        for n in nodes
        if (rs := store.node_recency_score(n.node_id)) is not None
    }
    reranked = UserFactsReranker(
        RerankWeights(kind=0.0, confidence=0.0, recency=1.0, bm25=0.0,
                      drift=0.0)
    ).score(nodes, SessionContext(), recency_scores=recency)
    assert reranked[0].node.node_id == "fresh"
