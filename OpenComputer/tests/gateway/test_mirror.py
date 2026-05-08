"""Tests for opencomputer/gateway/mirror.py — mirror_to_session.

PR-2 Task B6 of the messaging-gateway parity plan. Mirrors Hermes
``gateway/mirror.py`` semantics without copying code.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.gateway.dispatch import session_id_for
from opencomputer.gateway.mirror import mirror_to_session

# ── Session lookup misses ────────────────────────────────────────────────


def test_no_matching_session_returns_false(tmp_path, monkeypatch):
    """No session for (platform, chat_id) → returns False, no writes."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    db_path = tmp_path / "sessions.db"
    SessionDB(db_path)  # initialise schema only

    ok = mirror_to_session(
        platform="telegram",
        chat_id="nonexistent",
        message_text="hi",
        source_label="cli",
    )
    assert ok is False


# ── JSONL append ────────────────────────────────────────────────────────


def test_match_appends_to_jsonl(tmp_path, monkeypatch):
    """Matching session → JSONL row appended with mirror=True."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)

    sid = session_id_for("telegram", "555")
    db.create_session(sid, platform="telegram")

    ok = mirror_to_session(
        platform="telegram",
        chat_id="555",
        message_text="hello world",
        source_label="cron",
    )
    assert ok is True

    jsonl_path = tmp_path / "sessions" / f"{sid}.jsonl"
    assert jsonl_path.exists()
    line = jsonl_path.read_text(encoding="utf-8").strip()
    obj = json.loads(line)
    assert obj["role"] == "assistant"
    assert obj["content"] == "hello world"
    assert obj["mirror"] is True
    assert obj["mirror_source"] == "cron"


# ── SQLite append ───────────────────────────────────────────────────────


def test_match_appends_to_sqlite(tmp_path, monkeypatch):
    """Matching session → SQLite messages row appended."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)

    sid = session_id_for("telegram", "777")
    db.create_session(sid, platform="telegram")

    ok = mirror_to_session(
        platform="telegram",
        chat_id="777",
        message_text="from-cron",
        source_label="cron",
    )
    assert ok is True
    msgs = db.get_messages(sid)
    assert len(msgs) == 1
    assert msgs[0].role == "assistant"
    assert msgs[0].content == "from-cron"


# ── Best-effort: errors swallowed ────────────────────────────────────────


def test_best_effort_swallows_errors(tmp_path, monkeypatch):
    """Even when JSONL write fails, mirror_to_session returns gracefully (no crash)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    sid = session_id_for("telegram", "AA")
    db.create_session(sid, platform="telegram")

    # Make the sessions dir a regular file so JSONL writes fail.
    sessions_dir = tmp_path / "sessions"
    if sessions_dir.exists():
        for child in sessions_dir.iterdir():
            child.unlink()
        sessions_dir.rmdir()
    sessions_dir.write_text("not a directory")

    # Should not raise. Return value may be True or False — best-effort.
    try:
        result = mirror_to_session(
            platform="telegram",
            chat_id="AA",
            message_text="hi",
            source_label="cli",
        )
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"mirror_to_session raised: {e!r}")
    # Whatever we got back, the SQLite row may still have made it.
    # Function must not have raised.
    assert result in (True, False)


# ── thread_id filter ────────────────────────────────────────────────────


def test_thread_id_filter_picks_thread_session(tmp_path, monkeypatch):
    """thread_id arg routes to thread-specific session, not the base chat."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)

    sid_chat = session_id_for("telegram", "C9")
    sid_thread = session_id_for("telegram", "C9", thread_hint="T3")
    db.create_session(sid_chat, platform="telegram")
    db.create_session(sid_thread, platform="telegram")

    ok = mirror_to_session(
        platform="telegram",
        chat_id="C9",
        message_text="thread-msg",
        source_label="cli",
        thread_id="T3",
    )
    assert ok is True
    msgs_thread = db.get_messages(sid_thread)
    msgs_base = db.get_messages(sid_chat)
    assert len(msgs_thread) == 1
    assert msgs_thread[0].content == "thread-msg"
    assert len(msgs_base) == 0


# ── user_id preference ──────────────────────────────────────────────────


def test_user_id_preferred_when_provided(tmp_path, monkeypatch):
    """When user_id is provided, the user-namespaced session id is preferred over the chat-only one."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)

    # Two sessions: chat-only and chat+user. Both exist.
    sid_chat_only = session_id_for("telegram", "GROUP1")
    sid_user = session_id_for("telegram", "GROUP1", thread_hint="user:U1")
    db.create_session(sid_chat_only, platform="telegram")
    db.create_session(sid_user, platform="telegram")

    ok = mirror_to_session(
        platform="telegram",
        chat_id="GROUP1",
        message_text="hi-from-cli",
        source_label="cli",
        user_id="U1",
    )
    assert ok is True
    user_msgs = db.get_messages(sid_user)
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "hi-from-cli"


# ── multiple matching sessions → False ─────────────────────────────────


def test_multiple_matches_returns_false(tmp_path, monkeypatch):
    """When multiple thread variants exist for a chat and no thread_id was given,
    we don't guess — return False so callers know mirror was skipped."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)

    # Two sessions on the same chat, different threads.
    sid_thread_a = session_id_for("telegram", "X", thread_hint="A")
    sid_thread_b = session_id_for("telegram", "X", thread_hint="B")
    db.create_session(sid_thread_a, platform="telegram")
    db.create_session(sid_thread_b, platform="telegram")

    # No thread_id — base chat session DOES NOT exist; multiple thread
    # variants do. Behavior: return False, write nothing.
    ok = mirror_to_session(
        platform="telegram",
        chat_id="X",
        message_text="ambiguous",
        source_label="cli",
    )
    assert ok is False
    assert len(db.get_messages(sid_thread_a)) == 0
    assert len(db.get_messages(sid_thread_b)) == 0
