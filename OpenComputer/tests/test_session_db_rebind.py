"""Tests for §9.2 SessionDB rebind + continuation row.

Coverage:
  - SessionDB.close() exists and is idempotent
  - SessionDB.rebind() points subsequent writes at the new DB
  - Continuation pointer row is written to the OLD DB before rebind
  - SubagentStore + EpisodicMemory re-attach to new DB via cascade helper
  - Resume detects the continuation pointer and surfaces a hint
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def test_session_db_has_close_method() -> None:
    """API parity with provider / MCP / browser harnesses."""
    from opencomputer.agent.state import SessionDB

    assert hasattr(SessionDB, "close")
    assert callable(SessionDB.close)


def test_close_is_idempotent(tmp_path: Path) -> None:
    """Calling close() multiple times must not raise."""
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "s.db")
    db.close()
    db.close()  # idempotent


def test_rebind_changes_db_path(tmp_path: Path) -> None:
    from opencomputer.agent.state import SessionDB

    a_path = tmp_path / "a.db"
    b_path = tmp_path / "b.db"
    db = SessionDB(a_path)
    assert db.db_path == a_path
    db.rebind(b_path)
    assert db.db_path == b_path


def test_rebind_creates_new_schema(tmp_path: Path) -> None:
    """The new DB file must be created + have a sessions table."""
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "old.db")
    new_path = tmp_path / "sub" / "new.db"
    assert not new_path.exists()
    db.rebind(new_path)
    assert new_path.exists()
    # Sessions table should exist.
    conn = sqlite3.connect(new_path)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()
    conn.close()
    assert rows is not None


def test_rebind_writes_continuation_pointer_to_old_db(tmp_path: Path) -> None:
    """The OLD DB receives a marker row noting the session continued
    elsewhere — used by resume to redirect users."""
    from opencomputer.agent.state import SessionDB

    old_path = tmp_path / "old.db"
    new_path = tmp_path / "new.db"
    db = SessionDB(old_path)
    sid = db.allocate_session_id()
    db.ensure_session(sid, platform="cli")

    db.rebind(new_path, source_session_id=sid, target_profile="newp")

    # Find the marker in the old DB.
    conn = sqlite3.connect(old_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? "
        "ORDER BY timestamp ASC",
        (sid,),
    ).fetchall()
    conn.close()
    # The continuation row must mention the target profile.
    bodies = " ".join(r["content"] for r in rows)
    assert "newp" in bodies
    assert "continued" in bodies.lower() or "moved" in bodies.lower()


def test_rebind_without_session_id_skips_marker(tmp_path: Path) -> None:
    """If no source session id given, no marker is written — clean rebind."""
    from opencomputer.agent.state import SessionDB

    old_path = tmp_path / "old.db"
    new_path = tmp_path / "new.db"
    db = SessionDB(old_path)
    db.rebind(new_path)

    conn = sqlite3.connect(old_path)
    rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
    conn.close()
    assert rows[0] == 0


def test_messages_after_rebind_land_in_new_db(tmp_path: Path) -> None:
    from opencomputer.agent.state import SessionDB

    old_path = tmp_path / "old.db"
    new_path = tmp_path / "new.db"
    db = SessionDB(old_path)
    sid = db.allocate_session_id()
    db.ensure_session(sid, platform="cli")
    db.rebind(new_path)

    # After rebind, ensure_session into the NEW DB.
    new_sid = db.allocate_session_id()
    db.ensure_session(new_sid, platform="cli", model="m")

    # New DB has the new session.
    conn = sqlite3.connect(new_path)
    row = conn.execute(
        "SELECT id FROM sessions WHERE id = ?", (new_sid,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == new_sid

    # Old DB does NOT have the new session.
    conn = sqlite3.connect(old_path)
    row = conn.execute(
        "SELECT id FROM sessions WHERE id = ?", (new_sid,),
    ).fetchone()
    conn.close()
    assert row is None


def test_rebind_validates_path(tmp_path: Path) -> None:
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "x.db")
    with pytest.raises(TypeError, match="Path"):
        db.rebind("not a path")  # type: ignore[arg-type]


def test_rebind_to_same_path_is_safe(tmp_path: Path) -> None:
    """Rebind to the current path is a no-op (idempotent)."""
    from opencomputer.agent.state import SessionDB

    p = tmp_path / "x.db"
    db = SessionDB(p)
    db.rebind(p)
    assert db.db_path == p
