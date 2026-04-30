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


# ─── auto_prune ────────────────────────────────────────────────


def test_auto_prune_disabled_when_both_zero(db: SessionDB) -> None:
    _seed_session(db, "old")
    deleted = db.auto_prune(
        older_than_days=0, untitled_days=0, min_messages=3
    )
    assert deleted == 0
    assert db.get_session("old") is not None


def test_auto_prune_drops_old_sessions(db: SessionDB) -> None:
    _seed_session(db, "ancient", messages=5)
    _seed_session(db, "fresh", messages=5)
    with db._connect() as c:
        c.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 100 * 86400, "ancient"),
        )
    deleted = db.auto_prune(
        older_than_days=90, untitled_days=0, min_messages=3
    )
    assert deleted == 1
    assert db.get_session("ancient") is None
    assert db.get_session("fresh") is not None


def test_auto_prune_drops_untitled_empty_after_short_ttl(db: SessionDB) -> None:
    db.create_session("u1", platform="cli", model="m", title="")
    db.append_messages_batch("u1", [Message(role="user", content="hi")])
    with db._connect() as c:
        c.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 10 * 86400, "u1"),
        )
    deleted = db.auto_prune(
        older_than_days=0, untitled_days=7, min_messages=3
    )
    assert deleted == 1


def test_auto_prune_keeps_untitled_with_enough_messages(db: SessionDB) -> None:
    """Untitled but message-rich sessions survive the untitled policy."""
    db.create_session("u1", platform="cli", model="m", title="")
    db.append_messages_batch(
        "u1", [Message(role="user", content="hi") for _ in range(5)]
    )
    with db._connect() as c:
        c.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 10 * 86400, "u1"),
        )
    deleted = db.auto_prune(
        older_than_days=0, untitled_days=7, min_messages=3
    )
    assert deleted == 0
    assert db.get_session("u1") is not None


def test_auto_prune_caps_at_200(db: SessionDB) -> None:
    for i in range(250):
        db.create_session(f"old-{i}", platform="cli", model="m", title="")
        with db._connect() as c:
            c.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 100 * 86400, f"old-{i}"),
            )
    deleted = db.auto_prune(
        older_than_days=90, untitled_days=0, min_messages=3
    )
    assert deleted == 200
