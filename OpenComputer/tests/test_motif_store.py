"""Tests for :class:`opencomputer.inference.storage.MotifStore`."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from opencomputer.inference.storage import (
    SCHEMA_VERSION,
    MotifStore,
    apply_migrations,
)
from plugin_sdk.inference import Motif


@pytest.fixture(autouse=True)
def _isolate_bus():
    """Bus swap+restore — preserves the cross-file singleton invariant."""
    from opencomputer.ingestion import bus as bus_module
    from opencomputer.ingestion.bus import reset_default_bus

    saved = bus_module.default_bus
    reset_default_bus()
    yield
    bus_module.default_bus = saved


def _make_motif(
    *,
    kind: str = "temporal",
    summary: str = "test",
    confidence: float = 0.7,
    support: int = 5,
    created_at: float | None = None,
    session_id: str | None = None,
) -> Motif:
    return Motif(
        kind=kind,  # type: ignore[arg-type]
        confidence=confidence,
        support=support,
        summary=summary,
        payload={"foo": "bar", "n": 1},
        evidence_event_ids=("evt-1", "evt-2"),
        created_at=created_at if created_at is not None else time.time(),
        session_id=session_id,
    )


def test_insert_and_get_round_trip(tmp_path: Path) -> None:
    """Insert a motif, get it back — every field round-trips exactly."""
    store = MotifStore(db_path=tmp_path / "m.sqlite")
    m = _make_motif(kind="temporal", summary="round-trip", session_id="s1")
    store.insert(m)
    fetched = store.get(m.motif_id)
    assert fetched is not None
    assert fetched.motif_id == m.motif_id
    assert fetched.kind == "temporal"
    assert fetched.summary == "round-trip"
    assert fetched.confidence == m.confidence
    assert fetched.support == m.support
    assert fetched.payload == {"foo": "bar", "n": 1}
    assert fetched.evidence_event_ids == ("evt-1", "evt-2")
    assert fetched.session_id == "s1"


def test_list_filters_by_kind(tmp_path: Path) -> None:
    """``list(kind=...)`` returns only motifs of that kind."""
    store = MotifStore(db_path=tmp_path / "m.sqlite")
    store.insert(_make_motif(kind="temporal", summary="t1"))
    store.insert(_make_motif(kind="temporal", summary="t2"))
    store.insert(_make_motif(kind="transition", summary="x1"))
    assert {m.summary for m in store.list(kind="temporal")} == {"t1", "t2"}
    assert {m.summary for m in store.list(kind="transition")} == {"x1"}
    assert len(store.list()) == 3


def test_list_filters_by_since(tmp_path: Path) -> None:
    """``list(since=...)`` excludes motifs older than the cutoff."""
    store = MotifStore(db_path=tmp_path / "m.sqlite")
    now = time.time()
    store.insert(_make_motif(summary="ancient", created_at=now - 10000))
    store.insert(_make_motif(summary="recent", created_at=now - 5))
    out = store.list(since=now - 100)
    assert {m.summary for m in out} == {"recent"}


def test_list_orders_newest_first(tmp_path: Path) -> None:
    """``list`` orders by created_at DESC (newest first)."""
    store = MotifStore(db_path=tmp_path / "m.sqlite")
    now = time.time()
    store.insert(_make_motif(summary="oldest", created_at=now - 30))
    store.insert(_make_motif(summary="middle", created_at=now - 20))
    store.insert(_make_motif(summary="newest", created_at=now - 10))
    out = store.list()
    assert [m.summary for m in out] == ["newest", "middle", "oldest"]


def test_count_by_kind(tmp_path: Path) -> None:
    """``count`` and ``count(kind=...)`` return correct row counts."""
    store = MotifStore(db_path=tmp_path / "m.sqlite")
    store.insert(_make_motif(kind="temporal"))
    store.insert(_make_motif(kind="temporal"))
    store.insert(_make_motif(kind="transition"))
    assert store.count() == 3
    assert store.count(kind="temporal") == 2
    assert store.count(kind="transition") == 1
    assert store.count(kind="implicit_goal") == 0


def test_delete_older_than_removes_old(tmp_path: Path) -> None:
    """``delete_older_than`` removes only motifs older than the cutoff."""
    store = MotifStore(db_path=tmp_path / "m.sqlite")
    now = time.time()
    store.insert(_make_motif(summary="ancient", created_at=now - 10000))
    store.insert(_make_motif(summary="recent", created_at=now - 5))
    deleted = store.delete_older_than(100)
    assert deleted == 1
    remaining = store.list()
    assert {m.summary for m in remaining} == {"recent"}


def test_insert_many_atomic_count(tmp_path: Path) -> None:
    """``insert_many`` returns the number written."""
    store = MotifStore(db_path=tmp_path / "m.sqlite")
    motifs = [_make_motif(summary=f"m{i}") for i in range(5)]
    n = store.insert_many(motifs)
    assert n == 5
    assert store.count() == 5
    # Empty list is a no-op.
    assert store.insert_many([]) == 0


def test_schema_migration_idempotent(tmp_path: Path) -> None:
    """Re-applying migrations on a current DB is a no-op."""
    db = tmp_path / "m.sqlite"
    store = MotifStore(db_path=db)
    store.insert(_make_motif())
    # Re-run migrations against the same DB — must not duplicate
    # schema_version rows or destroy data.
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        apply_migrations(conn)
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == SCHEMA_VERSION
    finally:
        conn.close()
    # And data still readable through the store.
    assert store.count() == 1


def test_get_missing_returns_none(tmp_path: Path) -> None:
    """``get`` of an unknown id returns ``None``."""
    store = MotifStore(db_path=tmp_path / "m.sqlite")
    assert store.get("does-not-exist") is None
