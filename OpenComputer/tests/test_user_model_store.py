"""Tests for :class:`opencomputer.user_model.store.UserModelStore`."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from opencomputer.user_model.store import (
    SCHEMA_VERSION,
    UserModelStore,
    apply_migrations,
)
from plugin_sdk.user_model import Edge, Node


def _store(tmp_path: Path) -> UserModelStore:
    return UserModelStore(db_path=tmp_path / "graph.sqlite")


def test_insert_and_get_node_round_trip(tmp_path: Path) -> None:
    """Insert a node, get it back — every field round-trips exactly."""
    store = _store(tmp_path)
    n = Node(
        kind="attribute",
        value="uses Python",
        confidence=0.7,
        metadata={"hello": "world"},
    )
    store.insert_node(n)
    fetched = store.get_node(n.node_id)
    assert fetched is not None
    assert fetched.node_id == n.node_id
    assert fetched.kind == "attribute"
    assert fetched.value == "uses Python"
    assert fetched.confidence == 0.7
    assert fetched.metadata == {"hello": "world"}
    assert fetched.created_at == n.created_at


def test_upsert_node_bumps_last_seen(tmp_path: Path) -> None:
    """Same (kind, value) upsert keeps ``node_id`` and bumps ``last_seen_at``."""
    store = _store(tmp_path)
    n1 = store.upsert_node(kind="attribute", value="uses Python", confidence=0.5)
    # Sleep a tiny bit so the timestamp can advance.
    time.sleep(0.01)
    n2 = store.upsert_node(kind="attribute", value="uses Python", confidence=0.9)
    assert n1.node_id == n2.node_id
    assert n2.last_seen_at >= n1.last_seen_at
    # Confidence takes the max — never decreases on upsert.
    assert n2.confidence == 0.9
    # And only one node was materialised.
    assert store.count_nodes() == 1


def test_upsert_node_never_lowers_confidence(tmp_path: Path) -> None:
    """An upsert with a *lower* confidence preserves the higher one."""
    store = _store(tmp_path)
    store.upsert_node(kind="attribute", value="uses Python", confidence=0.9)
    updated = store.upsert_node(kind="attribute", value="uses Python", confidence=0.2)
    assert updated.confidence == 0.9


def test_list_nodes_filters_by_kind(tmp_path: Path) -> None:
    """``list_nodes(kinds=[...])`` only returns matching nodes."""
    store = _store(tmp_path)
    store.upsert_node(kind="attribute", value="a1")
    store.upsert_node(kind="attribute", value="a2")
    store.upsert_node(kind="preference", value="p1")
    store.upsert_node(kind="goal", value="g1")
    assert {n.value for n in store.list_nodes(kinds=["attribute"])} == {"a1", "a2"}
    assert {n.value for n in store.list_nodes(kinds=["preference", "goal"])} == {
        "p1",
        "g1",
    }
    assert len(store.list_nodes()) == 4


def test_search_fts5_finds_match(tmp_path: Path) -> None:
    """FTS5 query matches against ``node.value``."""
    store = _store(tmp_path)
    store.upsert_node(kind="attribute", value="uses Python")
    store.upsert_node(kind="attribute", value="prefers JavaScript over Rust")
    hits = store.search_nodes_fts("Python")
    assert {h.value for h in hits} == {"uses Python"}
    hits2 = store.search_nodes_fts("Rust")
    assert {h.value for h in hits2} == {"prefers JavaScript over Rust"}


def test_search_fts5_returns_empty_on_no_match(tmp_path: Path) -> None:
    """No-match query returns an empty list (not an exception)."""
    store = _store(tmp_path)
    store.upsert_node(kind="attribute", value="uses Python")
    assert store.search_nodes_fts("quantum cryptography") == []
    # Empty query is also safe.
    assert store.search_nodes_fts("") == []


def test_insert_edge_with_fk_constraint(tmp_path: Path) -> None:
    """FK violation on missing endpoint raises ``IntegrityError``."""
    import pytest

    store = _store(tmp_path)
    n1 = store.upsert_node(kind="attribute", value="a")
    n2 = store.upsert_node(kind="attribute", value="b")
    good = Edge(kind="asserts", from_node=n1.node_id, to_node=n2.node_id)
    store.insert_edge(good)
    assert store.count_edges() == 1

    # Missing endpoint must fail fast under the FK.
    bad = Edge(kind="asserts", from_node="missing", to_node=n2.node_id)
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_edge(bad)


def test_delete_node_cascades_to_edges(tmp_path: Path) -> None:
    """Deleting a node drops all incident edges via ``ON DELETE CASCADE``."""
    store = _store(tmp_path)
    n1 = store.upsert_node(kind="attribute", value="a")
    n2 = store.upsert_node(kind="attribute", value="b")
    n3 = store.upsert_node(kind="attribute", value="c")
    store.insert_edge(Edge(kind="asserts", from_node=n1.node_id, to_node=n2.node_id))
    store.insert_edge(Edge(kind="asserts", from_node=n2.node_id, to_node=n3.node_id))
    assert store.count_edges() == 2

    deleted = store.delete_node(n2.node_id)
    assert deleted == 1
    # Both incident edges gone; the unrelated nodes survive.
    assert store.count_edges() == 0
    assert store.count_nodes() == 2


def test_schema_migration_idempotent(tmp_path: Path) -> None:
    """Re-applying migrations on a current DB is a no-op."""
    db = tmp_path / "graph.sqlite"
    store = _store(tmp_path)
    store.upsert_node(kind="attribute", value="baseline")
    conn = sqlite3.connect(str(db))
    try:
        apply_migrations(conn)
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == SCHEMA_VERSION
    finally:
        conn.close()
    # Data still readable through the store.
    assert store.count_nodes() == 1


def test_count_methods(tmp_path: Path) -> None:
    """``count_nodes`` and ``count_edges`` with + without kind filters."""
    store = _store(tmp_path)
    a = store.upsert_node(kind="attribute", value="a")
    b = store.upsert_node(kind="attribute", value="b")
    g = store.upsert_node(kind="goal", value="g")
    store.insert_edge(Edge(kind="asserts", from_node=a.node_id, to_node=b.node_id))
    store.insert_edge(Edge(kind="derives_from", from_node=g.node_id, to_node=a.node_id))
    assert store.count_nodes() == 3
    assert store.count_nodes(kinds=["attribute"]) == 2
    assert store.count_nodes(kinds=["goal"]) == 1
    assert store.count_edges() == 2
    assert store.count_edges(kinds=["asserts"]) == 1
    assert store.count_edges(kinds=["derives_from"]) == 1
    assert store.count_edges(kinds=["supersedes"]) == 0


def test_get_missing_returns_none(tmp_path: Path) -> None:
    """Missing ids return None on both lookups."""
    store = _store(tmp_path)
    assert store.get_node("no-such-node") is None
    assert store.get_edge("no-such-edge") is None


def test_list_edges_filters(tmp_path: Path) -> None:
    """``list_edges`` honours kind + from_node + to_node filters."""
    store = _store(tmp_path)
    a = store.upsert_node(kind="attribute", value="a")
    b = store.upsert_node(kind="attribute", value="b")
    c = store.upsert_node(kind="attribute", value="c")
    e1 = Edge(kind="asserts", from_node=a.node_id, to_node=b.node_id)
    e2 = Edge(kind="derives_from", from_node=b.node_id, to_node=c.node_id)
    e3 = Edge(kind="asserts", from_node=c.node_id, to_node=a.node_id)
    for e in (e1, e2, e3):
        store.insert_edge(e)
    assert len(store.list_edges(kind="asserts")) == 2
    assert len(store.list_edges(from_node=a.node_id)) == 1
    assert len(store.list_edges(to_node=a.node_id)) == 1


def test_update_edge_recency_weight(tmp_path: Path) -> None:
    """``update_edge_recency_weight`` writes a clamped value."""
    store = _store(tmp_path)
    a = store.upsert_node(kind="attribute", value="a")
    b = store.upsert_node(kind="attribute", value="b")
    edge = Edge(
        kind="asserts",
        from_node=a.node_id,
        to_node=b.node_id,
        recency_weight=1.0,
    )
    store.insert_edge(edge)
    store.update_edge_recency_weight(edge.edge_id, 0.25)
    fetched = store.get_edge(edge.edge_id)
    assert fetched is not None
    assert fetched.recency_weight == 0.25
    # Out-of-range values are clamped.
    store.update_edge_recency_weight(edge.edge_id, 5.0)
    fetched2 = store.get_edge(edge.edge_id)
    assert fetched2 is not None
    assert fetched2.recency_weight == 1.0
    store.update_edge_recency_weight(edge.edge_id, -0.5)
    fetched3 = store.get_edge(edge.edge_id)
    assert fetched3 is not None
    assert fetched3.recency_weight == 0.0
