"""Tests for the F4 ``edges.source`` provenance column (Phase 4 — catch-up plan).

Source tagging is the foundation for the F4 ↔ Honcho hybrid: once edges
are tagged, MemoryBridge can feed motif-derived edges to Honcho one-way
without re-ingesting Honcho's own synthesis claims (cycle prevention).

This test file covers ONLY the schema + importer write path. Bridge
plumbing and the bidirectional flow are scoped to a follow-up phase
(4.B) that lands when a real Honcho instance is in CI.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from opencomputer.user_model.store import (
    SCHEMA_VERSION,
    UserModelStore,
    apply_migrations,
)
from plugin_sdk.user_model import Edge, Node


@pytest.fixture
def store(tmp_path):
    """Open a UserModelStore at a fresh temp path."""
    db_path = tmp_path / "user_model.sqlite"
    return UserModelStore(db_path)


def _seed_two_nodes(store: UserModelStore) -> tuple[str, str]:
    a = store.upsert_node(kind="attribute", value="A", confidence=0.8)
    b = store.upsert_node(kind="attribute", value="B", confidence=0.8)
    return a.node_id, b.node_id


def test_schema_version_is_v2():
    assert SCHEMA_VERSION == 2


def test_edges_table_has_source_column(store: UserModelStore, tmp_path):
    cols = {
        row[1]
        for row in store._connect().__enter__().execute("PRAGMA table_info(edges)").fetchall()  # type: ignore[attr-defined]
    }
    assert "source" in cols


def test_idx_edges_source_exists(store: UserModelStore):
    conn = store._connect().__enter__()  # type: ignore[attr-defined]
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_edges_source'"
    ).fetchall()
    assert len(rows) == 1


def test_default_edge_source_is_unknown(store: UserModelStore):
    a, b = _seed_two_nodes(store)
    edge = Edge(kind="asserts", from_node=a, to_node=b, salience=0.5, confidence=0.5)
    store.insert_edge(edge)
    fetched = store.get_edge(edge.edge_id)
    assert fetched is not None
    assert fetched.source == "unknown"


def test_explicit_edge_source_round_trips(store: UserModelStore):
    a, b = _seed_two_nodes(store)
    edge = Edge(
        kind="asserts",
        from_node=a, to_node=b,
        salience=0.5, confidence=0.5,
        source="motif_importer",
    )
    store.insert_edge(edge)
    assert store.get_edge(edge.edge_id).source == "motif_importer"


def test_honcho_synthesis_source_round_trips(store: UserModelStore):
    """Once Phase 4.B wires the bridge, Honcho-derived edges land with
    ``source='honcho_synthesis'`` so the importer can skip them in its
    own one-way feed (cycle prevention)."""
    a, b = _seed_two_nodes(store)
    edge = Edge(
        kind="asserts",
        from_node=a, to_node=b,
        salience=0.4,
        confidence=0.4,
        source="honcho_synthesis",
    )
    store.insert_edge(edge)
    assert store.get_edge(edge.edge_id).source == "honcho_synthesis"


def test_legacy_v1_db_migrates_in_place(tmp_path):
    """A v1 DB written by an older client must migrate to v2 on open
    without data loss. Edges from v1 land with source='unknown'."""
    db_path = tmp_path / "legacy.sqlite"

    # Build a v1-shaped database manually (no source column).
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (1);
        CREATE TABLE nodes (
            node_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            confidence REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE edges (
            edge_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            from_node TEXT NOT NULL,
            to_node TEXT NOT NULL,
            salience REAL NOT NULL,
            confidence REAL NOT NULL,
            recency_weight REAL NOT NULL,
            source_reliability REAL NOT NULL,
            decay_rate REAL NOT NULL,
            created_at REAL NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    now = time.time()
    conn.execute(
        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("n1", "attribute", "A", now, now, 0.8, "{}"),
    )
    conn.execute(
        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("n2", "attribute", "B", now, now, 0.8, "{}"),
    )
    conn.execute(
        "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("e1", "asserts", "n1", "n2", 0.5, 0.5, 1.0, 0.5, 0.01, now, "{}"),
    )
    conn.commit()
    conn.close()

    # Apply migrations — should advance to v2.
    conn = sqlite3.connect(db_path)
    apply_migrations(conn)
    cur = conn.execute("SELECT version FROM schema_version").fetchone()
    assert cur[0] == 2

    # Pre-existing edge keeps its data + has source='unknown'.
    cur = conn.execute("SELECT source FROM edges WHERE edge_id='e1'").fetchone()
    assert cur[0] == "unknown"
    conn.close()


def test_motif_importer_tags_edges_with_motif_importer_source():
    """Smoke test that the importer constructs edges with the right tag.

    We don't run the full importer here (needs MotifStore fixtures);
    instead we verify the constants land in the constructor by source-
    inspecting the importer module — a regression guard against future
    refactors that drop the tag.
    """
    import opencomputer.user_model.importer as imp_mod
    src = imp_mod.__file__

    with open(src) as fp:
        text = fp.read()

    # All three Edge( ... ) constructors in the importer must include
    # ``source="motif_importer"`` after Phase 4 source-tagging lands.
    assert text.count('source="motif_importer"') >= 3, (
        "expected the motif importer to tag at least three edge "
        "constructors with source='motif_importer'"
    )
