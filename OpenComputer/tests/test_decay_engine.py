"""Tests for :class:`opencomputer.user_model.decay.DecayEngine`.

Phase 3.D — temporal decay over the user-model edges table. The engine
applies an exponential half-life formula keyed on the edge kind; tests
pin both the per-kind half-life lookup and the numeric behaviour of the
formula itself, then verify :meth:`apply_decay` persists through
:class:`UserModelStore`.
"""

from __future__ import annotations

import time
from pathlib import Path

from opencomputer.user_model.decay import DecayEngine
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.decay import DecayConfig
from plugin_sdk.user_model import Edge, Node


def _store(tmp_path: Path) -> UserModelStore:
    return UserModelStore(db_path=tmp_path / "graph.sqlite")


def _seed_node(store: UserModelStore, value: str) -> Node:
    return store.upsert_node(kind="attribute", value=value)


def _seed_edge(
    store: UserModelStore,
    *,
    from_node: str,
    to_node: str,
    kind: str = "asserts",
    age_days: float = 0.0,
    now: float | None = None,
) -> Edge:
    """Create + persist one edge whose ``created_at`` is ``age_days`` in the past."""
    now_ts = time.time() if now is None else now
    created_at = now_ts - age_days * 86400.0
    edge = Edge(
        kind=kind,  # type: ignore[arg-type]
        from_node=from_node,
        to_node=to_node,
        created_at=created_at,
    )
    store.insert_edge(edge)
    return edge


def test_half_life_per_kind_uses_config(tmp_path: Path) -> None:
    """``half_life_for`` returns the per-kind value from the config."""
    config = DecayConfig(
        asserts_half_life_days=10.0,
        contradicts_half_life_days=20.0,
        supersedes_half_life_days=30.0,
        derives_from_half_life_days=40.0,
        default_half_life_days=99.0,
    )
    engine = DecayEngine(store=_store(tmp_path), config=config)
    assert engine.half_life_for("asserts") == 10.0
    assert engine.half_life_for("contradicts") == 20.0
    assert engine.half_life_for("supersedes") == 30.0
    assert engine.half_life_for("derives_from") == 40.0
    # Unknown kinds fall back to the default.
    assert engine.half_life_for("uncharted") == 99.0


def test_compute_recency_weight_zero_age_returns_one(tmp_path: Path) -> None:
    """A freshly-created edge has recency weight = 1.0."""
    engine = DecayEngine(store=_store(tmp_path))
    now = time.time()
    edge = Edge(kind="asserts", created_at=now)
    assert engine.compute_recency_weight(edge, now=now) == 1.0


def test_compute_recency_weight_after_one_half_life_returns_half(tmp_path: Path) -> None:
    """Age == half_life days → weight ≈ 0.5."""
    config = DecayConfig(asserts_half_life_days=10.0)
    engine = DecayEngine(store=_store(tmp_path), config=config)
    now = time.time()
    edge = Edge(kind="asserts", created_at=now - 10.0 * 86400.0)
    weight = engine.compute_recency_weight(edge, now=now)
    assert abs(weight - 0.5) < 1e-9


def test_compute_recency_weight_floors_at_min(tmp_path: Path) -> None:
    """Very old edges never drop below ``min_recency_weight``."""
    config = DecayConfig(asserts_half_life_days=10.0, min_recency_weight=0.05)
    engine = DecayEngine(store=_store(tmp_path), config=config)
    now = time.time()
    # 10 000 days old → exponential result well below the floor.
    edge = Edge(kind="asserts", created_at=now - 10_000.0 * 86400.0)
    assert engine.compute_recency_weight(edge, now=now) == 0.05


def test_apply_decay_updates_all_edges(tmp_path: Path) -> None:
    """Seed edges of various ages, apply decay, confirm every row changed."""
    store = _store(tmp_path)
    config = DecayConfig(
        asserts_half_life_days=10.0,
        min_recency_weight=0.05,
    )
    engine = DecayEngine(store=store, config=config)
    now = time.time()
    # Five nodes + five edges at different ages.
    nodes = [_seed_node(store, f"v{i}") for i in range(5)]
    ages = [0.0, 5.0, 10.0, 20.0, 10_000.0]
    edges = [
        _seed_edge(
            store,
            from_node=nodes[i].node_id,
            to_node=nodes[(i + 1) % 5].node_id,
            age_days=ages[i],
            now=now,
        )
        for i in range(5)
    ]
    updated = engine.apply_decay(now=now, batch_size=3)
    assert updated == 5
    for edge, age in zip(edges, ages, strict=True):
        fetched = store.get_edge(edge.edge_id)
        assert fetched is not None
        expected = engine.compute_recency_weight(edge, now=now)
        # SQLite stores REAL — allow a tiny tolerance for float round-trip.
        assert abs(fetched.recency_weight - expected) < 1e-6
        # Sanity: weights match the formula monotonically for younger→older.
        if age == 0.0:
            assert fetched.recency_weight == 1.0
        if age == 10_000.0:
            assert fetched.recency_weight == 0.05


def test_apply_decay_for_node_only_touches_incident(tmp_path: Path) -> None:
    """``apply_decay_for_node(A)`` never updates edges that don't touch A."""
    store = _store(tmp_path)
    engine = DecayEngine(store=store, config=DecayConfig(asserts_half_life_days=10.0))
    now = time.time()
    a = _seed_node(store, "A")
    b = _seed_node(store, "B")
    c = _seed_node(store, "C")
    d = _seed_node(store, "D")
    # A has 2 edges (one outgoing to B, one incoming from C).
    edge_ab = _seed_edge(
        store, from_node=a.node_id, to_node=b.node_id, age_days=30.0, now=now
    )
    edge_ca = _seed_edge(
        store, from_node=c.node_id, to_node=a.node_id, age_days=20.0, now=now
    )
    # B has 3 additional unrelated edges (not incident to A).
    edge_bc = _seed_edge(
        store, from_node=b.node_id, to_node=c.node_id, age_days=30.0, now=now
    )
    edge_bd = _seed_edge(
        store, from_node=b.node_id, to_node=d.node_id, age_days=30.0, now=now
    )
    edge_db = _seed_edge(
        store, from_node=d.node_id, to_node=b.node_id, age_days=30.0, now=now
    )
    updated = engine.apply_decay_for_node(a.node_id, now=now)
    assert updated == 2
    # Incident edges: recency_weight has moved off the default 1.0.
    assert store.get_edge(edge_ab.edge_id).recency_weight < 1.0  # type: ignore[union-attr]
    assert store.get_edge(edge_ca.edge_id).recency_weight < 1.0  # type: ignore[union-attr]
    # Non-incident edges keep the default weight.
    for eid in (edge_bc.edge_id, edge_bd.edge_id, edge_db.edge_id):
        fetched = store.get_edge(eid)
        assert fetched is not None
        assert fetched.recency_weight == 1.0
