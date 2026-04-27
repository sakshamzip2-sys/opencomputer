"""Tests for the Tier-A item 14 additions to ``opencomputer mcp serve``.

Two new tools:

- ``channels_list`` — distinct platforms with at least one session.
- ``events_poll`` — incremental cursor-based poll for new messages.

The existing 5 read-only tools have their own coverage in
``test_mcp_server.py``; this file adds focused tests for the
new surface only.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from opencomputer.mcp.server import build_server


@pytest.fixture
def populated_db(tmp_path, monkeypatch):
    """Build a tiny sessions.db with three sessions across two platforms."""
    db = tmp_path / "sessions.db"
    monkeypatch.setattr(
        "opencomputer.mcp.server._home", lambda: tmp_path,
    )

    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                ended_at REAL,
                platform TEXT NOT NULL,
                model TEXT,
                title TEXT,
                message_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_call_id TEXT,
                tool_calls TEXT,
                name TEXT,
                reasoning TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT,
                timestamp REAL NOT NULL
            );
            """
        )
        now = time.time()
        conn.executemany(
            "INSERT INTO sessions (id, started_at, platform, model) "
            "VALUES (?, ?, ?, ?)",
            [
                ("sess-tg-1", now - 3600, "telegram", "claude-sonnet-4-7"),
                ("sess-tg-2", now - 1800, "telegram", "claude-sonnet-4-7"),
                ("sess-cli-1", now - 7200, "cli", "claude-sonnet-4-7"),
            ],
        )
        conn.executemany(
            "INSERT INTO messages "
            "(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            [
                ("sess-tg-1", "user", "hello from telegram", now - 3500),
                ("sess-tg-1", "assistant", "hi back", now - 3490),
                ("sess-tg-2", "user", "another telegram session", now - 1700),
                ("sess-cli-1", "user", "from CLI", now - 7100),
            ],
        )
        conn.commit()

    return db


# ──────────────────────────── channels_list ────────────────────────────


def test_channels_list_returns_distinct_platforms(populated_db):
    server = build_server()
    fn = _get_tool_fn(server, "channels_list")
    out = fn()
    platforms = {row["platform"] for row in out}
    assert platforms == {"telegram", "cli"}


def test_channels_list_session_count(populated_db):
    server = build_server()
    fn = _get_tool_fn(server, "channels_list")
    out = fn()
    by_platform = {row["platform"]: row for row in out}
    assert by_platform["telegram"]["session_count"] == 2
    assert by_platform["cli"]["session_count"] == 1


def test_channels_list_empty_when_no_db(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.mcp.server._home", lambda: tmp_path)
    server = build_server()
    fn = _get_tool_fn(server, "channels_list")
    assert fn() == []


# ──────────────────────────── events_poll ────────────────────────────


def test_events_poll_initial_returns_all(populated_db):
    server = build_server()
    fn = _get_tool_fn(server, "events_poll")
    result = fn(since_message_id=0, limit=100)
    assert len(result["messages"]) == 4
    # next_cursor should be the highest id returned
    assert result["next_cursor"] == result["messages"][-1]["id"]


def test_events_poll_with_cursor_returns_only_newer(populated_db):
    server = build_server()
    fn = _get_tool_fn(server, "events_poll")
    first = fn(since_message_id=0, limit=2)
    assert len(first["messages"]) == 2
    second = fn(since_message_id=first["next_cursor"], limit=10)
    assert len(second["messages"]) == 2
    # No overlap
    first_ids = {m["id"] for m in first["messages"]}
    second_ids = {m["id"] for m in second["messages"]}
    assert first_ids.isdisjoint(second_ids)


def test_events_poll_no_new_messages_returns_same_cursor(populated_db):
    server = build_server()
    fn = _get_tool_fn(server, "events_poll")
    drained = fn(since_message_id=0, limit=100)
    cursor = drained["next_cursor"]
    again = fn(since_message_id=cursor, limit=10)
    assert again["messages"] == []
    assert again["next_cursor"] == cursor


def test_events_poll_includes_platform_join(populated_db):
    server = build_server()
    fn = _get_tool_fn(server, "events_poll")
    out = fn(since_message_id=0, limit=10)
    for row in out["messages"]:
        assert "platform" in row
        assert row["platform"] in ("telegram", "cli")


def test_events_poll_no_db_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("opencomputer.mcp.server._home", lambda: tmp_path)
    server = build_server()
    fn = _get_tool_fn(server, "events_poll")
    out = fn(since_message_id=0)
    assert out == {"messages": [], "next_cursor": 0}


# ──────────────────────────── helper ────────────────────────────


def _get_tool_fn(server, name: str):
    """Reach into FastMCP's tool registry to invoke a tool by name."""
    # FastMCP stores tools in an internal attribute; this is the same
    # pattern used by the existing test_mcp_server.py for 5-tool checks.
    tool_manager = server._tool_manager
    tool = tool_manager._tools.get(name)
    assert tool is not None, f"tool {name!r} not registered"
    return tool.fn
