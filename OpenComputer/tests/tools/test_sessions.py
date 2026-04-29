"""Tests for the Sessions read trio — SessionsList / SessionsHistory / SessionsStatus.

Sub-project 1.F-read of the OpenClaw Tier 1 port (2026-04-28). Read-only
agent-facing surface over ``SessionDB.list_sessions``, ``get_messages``,
``get_session``. Spawn / Send are deferred per the AMENDMENTS doc.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.tools.sessions import (
    SessionsHistory,
    SessionsList,
    SessionsStatus,
)
from plugin_sdk.core import Message, ToolCall

# ─── helpers ──────────────────────────────────────────────────────────


def _call(name: str, args: dict[str, Any]) -> ToolCall:
    return ToolCall(id=f"t-{name}", name=name, arguments=args)


def _run(coro):  # tiny await helper
    return asyncio.run(coro)


@pytest.fixture
def db_with_sessions():
    """A real SessionDB on a tmp file, pre-populated with two sessions
    so we exercise the actual SQL paths (no mock)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = SessionDB(Path(tmp) / "s.db")
        db.create_session("sess-a", platform="cli", title="Alpha")
        db.append_message("sess-a", Message(role="user", content="hello a"))
        db.append_message("sess-a", Message(role="assistant", content="hi a"))
        db.create_session("sess-b", platform="cli", title="Bravo")
        db.append_message("sess-b", Message(role="user", content="hello b"))
        yield db


# ─── SessionsList ─────────────────────────────────────────────────────


def test_sessions_list_returns_recent_sessions(db_with_sessions: SessionDB):
    tool = SessionsList(db_with_sessions)
    result = _run(tool.execute(_call("SessionsList", {"limit": 10})))
    assert result.is_error is False
    # The two ids we created should both appear in the rendered content.
    assert "sess-a" in result.content
    assert "sess-b" in result.content


def test_sessions_list_respects_limit():
    with tempfile.TemporaryDirectory() as tmp:
        db = SessionDB(Path(tmp) / "s.db")
        for i in range(5):
            db.create_session(f"s-{i}", platform="cli")
        tool = SessionsList(db)
        result = _run(tool.execute(_call("SessionsList", {"limit": 2})))
        assert result.is_error is False
        # Two rows present, the other three absent.
        present = [sid for sid in ("s-0", "s-1", "s-2", "s-3", "s-4") if sid in result.content]
        assert len(present) == 2


def test_sessions_list_default_limit_when_omitted(db_with_sessions: SessionDB):
    """Omitting ``limit`` should fall back to the default (20) — both
    pre-seeded rows still come back."""
    tool = SessionsList(db_with_sessions)
    result = _run(tool.execute(_call("SessionsList", {})))
    assert result.is_error is False
    assert "sess-a" in result.content
    assert "sess-b" in result.content


# ─── SessionsHistory ──────────────────────────────────────────────────


def test_sessions_history_returns_messages_sliced_to_limit():
    with tempfile.TemporaryDirectory() as tmp:
        db = SessionDB(Path(tmp) / "s.db")
        db.create_session("sid", platform="cli")
        for i in range(50):
            db.append_message("sid", Message(role="user", content=f"msg-{i}"))
        tool = SessionsHistory(db)
        result = _run(tool.execute(_call("SessionsHistory", {"session_id": "sid", "limit": 10})))
        assert result.is_error is False
        # Last 10 messages (40-49) should appear; earlier ones should not.
        assert "msg-49" in result.content
        assert "msg-40" in result.content
        assert "msg-39" not in result.content
        assert "msg-0" not in result.content


def test_sessions_history_returns_empty_for_no_messages():
    with tempfile.TemporaryDirectory() as tmp:
        db = SessionDB(Path(tmp) / "s.db")
        db.create_session("sid", platform="cli")
        tool = SessionsHistory(db)
        result = _run(tool.execute(_call("SessionsHistory", {"session_id": "sid"})))
        # Empty result is NOT an error — it's a valid "no messages" answer.
        assert result.is_error is False
        assert result.content == "[]"


def test_sessions_history_default_limit_30():
    """Default ``limit=30`` when caller omits it."""
    with tempfile.TemporaryDirectory() as tmp:
        db = SessionDB(Path(tmp) / "s.db")
        db.create_session("sid", platform="cli")
        for i in range(40):
            db.append_message("sid", Message(role="user", content=f"m-{i}"))
        tool = SessionsHistory(db)
        result = _run(tool.execute(_call("SessionsHistory", {"session_id": "sid"})))
        assert result.is_error is False
        # Last 30 — m-10 .. m-39 — m-9 must not appear.
        assert "m-39" in result.content
        assert "m-10" in result.content
        assert "m-9" not in result.content


# ─── SessionsStatus ───────────────────────────────────────────────────


def test_sessions_status_returns_session_info(db_with_sessions: SessionDB):
    tool = SessionsStatus(db_with_sessions)
    result = _run(tool.execute(_call("SessionsStatus", {"session_id": "sess-a"})))
    assert result.is_error is False
    # The dict-stringification should expose the session's id and title.
    assert "sess-a" in result.content
    assert "Alpha" in result.content


def test_sessions_status_unknown_session_is_error(db_with_sessions: SessionDB):
    tool = SessionsStatus(db_with_sessions)
    result = _run(tool.execute(_call("SessionsStatus", {"session_id": "does-not-exist"})))
    assert result.is_error is True
    assert "unknown session" in result.content
    assert "does-not-exist" in result.content


# ─── Schemas + registration ───────────────────────────────────────────


def test_schemas_have_expected_names_and_required_args(db_with_sessions: SessionDB):
    list_t = SessionsList(db_with_sessions)
    hist_t = SessionsHistory(db_with_sessions)
    stat_t = SessionsStatus(db_with_sessions)

    assert list_t.schema.name == "SessionsList"
    assert hist_t.schema.name == "SessionsHistory"
    assert stat_t.schema.name == "SessionsStatus"

    # SessionsHistory + Status both require session_id.
    assert hist_t.schema.parameters["required"] == ["session_id"]
    assert stat_t.schema.parameters["required"] == ["session_id"]
    # SessionsList has no required args.
    assert list_t.schema.parameters.get("required", []) == []


def test_all_three_tools_register_via_cli_path():
    """Importing the cli module + invoking the bundled-tool registration
    helper should expose all three Sessions* schemas in the registry.
    """
    from opencomputer.cli import _register_builtin_tools
    from opencomputer.tools.registry import registry

    _register_builtin_tools()
    names = set(registry.names())
    assert "SessionsList" in names, names
    assert "SessionsHistory" in names, names
    assert "SessionsStatus" in names, names
