"""SessionDB.delete_session — cascades messages + FTS + episodic + side tables."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "sessions.db")


def _seed_session(db: SessionDB, sid: str, *, messages: int = 3) -> None:
    db.create_session(sid, platform="cli", model="test-model", title=f"session-{sid}")
    msgs = [Message(role="user", content=f"msg {i}") for i in range(messages)]
    db.append_messages_batch(sid, msgs)


def test_delete_existing_session_returns_true(db: SessionDB) -> None:
    _seed_session(db, "s1")
    assert db.delete_session("s1") is True


def test_delete_unknown_session_returns_false(db: SessionDB) -> None:
    assert db.delete_session("does-not-exist") is False


def test_delete_removes_session_row(db: SessionDB) -> None:
    _seed_session(db, "s1")
    db.delete_session("s1")
    assert db.get_session("s1") is None


def test_delete_cascades_messages(db: SessionDB) -> None:
    _seed_session(db, "s1", messages=5)
    db.delete_session("s1")
    assert db.get_messages("s1") == []


def test_delete_cascades_messages_fts(db: SessionDB) -> None:
    _seed_session(db, "s1", messages=3)
    with db._connect() as c:
        before = c.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    assert before > 0
    db.delete_session("s1")
    with db._connect() as c:
        after = c.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE rowid IN "
            "(SELECT id FROM messages WHERE session_id = ?)",
            ("s1",),
        ).fetchone()[0]
    assert after == 0


def test_delete_does_not_touch_other_sessions(db: SessionDB) -> None:
    _seed_session(db, "keep", messages=2)
    _seed_session(db, "drop", messages=2)
    db.delete_session("drop")
    assert db.get_session("keep") is not None
    assert len(db.get_messages("keep")) == 2


def test_delete_clears_vibe_log(db: SessionDB) -> None:
    _seed_session(db, "s1")
    db.record_vibe("s1", "focused")
    db.record_vibe("s1", "creative")
    with db._connect() as c:
        before = c.execute(
            "SELECT COUNT(*) FROM vibe_log WHERE session_id = ?", ("s1",)
        ).fetchone()[0]
    assert before == 2
    db.delete_session("s1")
    with db._connect() as c:
        after = c.execute(
            "SELECT COUNT(*) FROM vibe_log WHERE session_id = ?", ("s1",)
        ).fetchone()[0]
    assert after == 0


def test_delete_clears_tool_usage(db: SessionDB) -> None:
    _seed_session(db, "s1")
    db.record_tool_usage(session_id="s1", tool="Read", outcome="success")
    with db._connect() as c:
        before = c.execute(
            "SELECT COUNT(*) FROM tool_usage WHERE session_id = ?", ("s1",)
        ).fetchone()[0]
    assert before == 1
    db.delete_session("s1")
    with db._connect() as c:
        after = c.execute(
            "SELECT COUNT(*) FROM tool_usage WHERE session_id = ?", ("s1",)
        ).fetchone()[0]
    assert after == 0
