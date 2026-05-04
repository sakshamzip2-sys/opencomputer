"""Tests for opencomputer.kanban.db — durable task board (Wave 6.B).

Hermes-port (c86842546). 2836 LOC of SQLite-backed kernel ported
verbatim with HERMES_* → OC_* env-var rename. These tests verify the
load-bearing contracts: connect/init, create/show/complete round-trip,
status transitions, link cycles rejected, idempotency keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.kanban import db


@pytest.fixture
def kdb(tmp_path: Path, monkeypatch):
    """Each test gets a fresh kanban.db in tmp_path."""
    monkeypatch.setenv("OC_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv(
        "OC_KANBAN_WORKSPACES_ROOT", str(tmp_path / "workspaces"),
    )
    db.init_db()
    return db


def test_init_db_idempotent(kdb):
    """Calling init_db twice on the same path is a no-op."""
    kdb.init_db()
    kdb.init_db()  # should not raise


def test_create_and_get_task(kdb):
    conn = kdb.connect()
    try:
        tid = kdb.create_task(
            conn,
            title="port the kanban",
            assignee="saksham",
        )
    finally:
        conn.close()
    assert tid is not None
    assert tid.startswith("t_")

    conn = kdb.connect()
    try:
        task = kdb.get_task(conn, tid)
    finally:
        conn.close()
    assert task is not None
    assert task.title == "port the kanban"
    assert task.assignee == "saksham"


def test_status_transitions(kdb):
    conn = kdb.connect()
    try:
        tid = kdb.create_task(conn, title="x", assignee="a")
        # ready by default for top-level tasks
        task = kdb.get_task(conn, tid)
        assert task.status in ("ready", "todo")

        # complete
        ok = kdb.complete_task(conn, tid, summary="all done")
        assert ok
        done = kdb.get_task(conn, tid)
        assert done.status == "done"
    finally:
        conn.close()


def test_idempotency_key_returns_existing_task(kdb):
    """A second create with the same idempotency_key returns the original id."""
    conn = kdb.connect()
    try:
        tid1 = kdb.create_task(
            conn, title="job1", assignee="a", idempotency_key="key-x",
        )
        tid2 = kdb.create_task(
            conn, title="job1-dup", assignee="a", idempotency_key="key-x",
        )
    finally:
        conn.close()
    assert tid1 == tid2


def test_link_creates_parent_child(kdb):
    conn = kdb.connect()
    try:
        parent = kdb.create_task(conn, title="parent", assignee="a")
        child = kdb.create_task(conn, title="child", assignee="a", triage=True)
        kdb.link_tasks(conn, parent_id=parent, child_id=child)
        kids = kdb.child_ids(conn, parent)
        assert child in kids
        rents = kdb.parent_ids(conn, child)
        assert parent in rents
    finally:
        conn.close()


def test_self_link_rejected(kdb):
    conn = kdb.connect()
    try:
        tid = kdb.create_task(conn, title="x", assignee="a")
        with pytest.raises(Exception):
            kdb.link_tasks(conn, parent_id=tid, child_id=tid)
    finally:
        conn.close()


def test_unknown_parent_link_rejected(kdb):
    conn = kdb.connect()
    try:
        tid = kdb.create_task(conn, title="x", assignee="a")
        with pytest.raises(Exception):
            kdb.link_tasks(conn, parent_id="nonexistent", child_id=tid)
    finally:
        conn.close()


def test_db_path_respects_env_override(kdb, tmp_path):
    """OC_KANBAN_DB env override pins the file path."""
    p = kdb.kanban_db_path()
    assert str(tmp_path) in str(p)
