"""Hermes parity G14: events_poll(after_cursor) alias + approval event types."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import patch

import pytest

from opencomputer.mcp.server import build_server


@pytest.fixture
def isolated_home(tmp_path):
    with patch("opencomputer.mcp.server._home", return_value=tmp_path):
        yield tmp_path


def _decode(result):
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return result[1].get("result", result[1])
    return result


def test_after_cursor_alias_works(isolated_home):
    """G14: after_cursor accepted alongside since_message_id."""
    server = build_server()
    result = asyncio.run(
        server.call_tool("events_poll", {"after_cursor": 0, "limit": 10})
    )
    data = _decode(result)
    assert isinstance(data, dict)
    assert "messages" in data
    assert "next_cursor" in data
    assert "approvals" in data


def test_legacy_since_message_id_still_works(isolated_home):
    """Back-compat: don't break clients using since_message_id."""
    server = build_server()
    result = asyncio.run(
        server.call_tool("events_poll", {"since_message_id": 0})
    )
    data = _decode(result)
    assert isinstance(data, dict)
    assert "messages" in data


def test_returns_approval_events_when_audit_log_present(isolated_home):
    """G14: when F1 audit_log has approval entries, surface them under approvals."""
    db_path = isolated_home / "sessions.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, platform TEXT, started_at REAL)"
        )
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY, session_id TEXT, "
            "role TEXT, content TEXT, timestamp REAL)"
        )
        conn.execute(
            "CREATE TABLE audit_log ("
            "id INTEGER PRIMARY KEY, ts REAL, capability_id TEXT, "
            "action TEXT, tier INTEGER, scope TEXT, granted_by TEXT)"
        )
        conn.execute(
            "INSERT INTO audit_log (ts, capability_id, action, tier, scope, granted_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1700000000.0, "fs.write", "granted", 1, "/tmp", "user"),
        )
        conn.execute(
            "INSERT INTO audit_log (ts, capability_id, action, tier, scope, granted_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1700000001.0, "shell.exec", "revoked", 1, "rm -rf", "user"),
        )
        conn.commit()

    server = build_server()
    result = asyncio.run(
        server.call_tool("events_poll", {"after_cursor": 0})
    )
    data = _decode(result)
    assert isinstance(data, dict)
    approvals = data.get("approvals", [])
    assert len(approvals) == 2
    assert {a["capability_id"] for a in approvals} == {"fs.write", "shell.exec"}
    types = {a["type"] for a in approvals}
    assert types == {"approval_resolved"}, (
        f"granted+revoked should both map to approval_resolved; got {types}"
    )


def test_no_audit_log_table_returns_empty_approvals(isolated_home):
    server = build_server()
    result = asyncio.run(
        server.call_tool("events_poll", {})
    )
    data = _decode(result)
    assert data.get("approvals") == []
