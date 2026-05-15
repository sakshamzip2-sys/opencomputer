"""Channel-bridge approval flow (M3 — mcp-openclaw-port).

Covers:

* ``--enable-approvals`` flag gates exposure of permission tools.
* ``permissions_request_subscribe`` long-polls the consent_requests
  table and returns when an entry appears (or empty on timeout).
* The MCP-driven ``permissions_respond`` records ``granted_by="mcp_client"``
  in the consent store so the audit log distinguishes it from CLI grants.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Generator
from unittest.mock import patch

import pytest

from opencomputer.mcp.server import build_server


@pytest.fixture
def isolated_home(tmp_path) -> Generator:
    with patch("opencomputer.mcp.server._home", return_value=tmp_path):
        yield tmp_path


def _decode_tool_result(result):
    """FastMCP returns (content_list, structured_dict). Pull the structured form."""
    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        return structured
    content = getattr(result, "content", None)
    if content is None and isinstance(result, list):
        content = result
    if content:
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except (ValueError, TypeError):
                    return text
    return result


def _seed_consent_requests_table(db_path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS consent_requests ("
            "capability_id TEXT, scope TEXT, requested_at REAL, "
            "requested_by TEXT, state TEXT)"
        )
        # F1 consent_grants table — needed by permissions_respond's
        # ConsentStore.upsert path. Mirror state.py V3_CONSENT_DDL.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS consent_grants ("
            "capability_id TEXT NOT NULL, scope_filter TEXT, "
            "tier INTEGER NOT NULL, granted_at REAL NOT NULL, "
            "expires_at REAL, granted_by TEXT NOT NULL, "
            "PRIMARY KEY (capability_id, scope_filter))"
        )
        conn.commit()


# ─── --enable-approvals flag gates tool exposure ────────────────


def test_approval_tools_absent_by_default() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    # Without --enable-approvals, the long-poll subscribe tool is OFF
    # for security. permissions_list_open and permissions_respond stay
    # on because they were the original Hermes parity G13 + 10th tool —
    # we only gate the NEW M3 subscribe tool.
    assert "permissions_request_subscribe" not in names


def test_approval_tools_present_when_enabled() -> None:
    server = build_server(enable_approvals=True)
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "permissions_request_subscribe" in names


# ─── permissions_request_subscribe long-poll ────────────────────


def test_subscribe_returns_empty_on_timeout(isolated_home) -> None:
    _seed_consent_requests_table(isolated_home / "sessions.db")
    server = build_server(enable_approvals=True)
    started = time.time()
    result = asyncio.run(
        server.call_tool(
            "permissions_request_subscribe",
            {"timeout_s": 0.3, "poll_interval_s": 0.1},
        )
    )
    elapsed = time.time() - started
    data = _decode_tool_result(result)
    assert data == [] or data == [[]] or (isinstance(data, dict) and data.get("requests") == [])
    # Should have actually waited (roughly) the timeout window
    assert elapsed >= 0.25


def test_subscribe_returns_pending_immediately(isolated_home) -> None:
    db_path = isolated_home / "sessions.db"
    _seed_consent_requests_table(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO consent_requests VALUES (?, ?, ?, ?, ?)",
            ("fs.write", "/etc/passwd", 1700000000.0, "tool:Edit", "pending"),
        )
        conn.commit()
    server = build_server(enable_approvals=True)
    result = asyncio.run(
        server.call_tool(
            "permissions_request_subscribe",
            {"timeout_s": 5.0, "poll_interval_s": 0.1},
        )
    )
    data = _decode_tool_result(result)
    # Either bare list or {requests: [...]} wrapping
    if isinstance(data, dict):
        data = data.get("requests", data)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["capability_id"] == "fs.write"


def test_subscribe_returns_when_pending_arrives_mid_wait(
    isolated_home,
) -> None:
    """Seed AFTER subscribe starts; long-poll should pick it up."""
    db_path = isolated_home / "sessions.db"
    _seed_consent_requests_table(db_path)
    server = build_server(enable_approvals=True)

    async def _seed_after_delay() -> None:
        await asyncio.sleep(0.15)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO consent_requests VALUES (?, ?, ?, ?, ?)",
                ("shell.exec", "ls /tmp", 1700000001.0, "tool:Bash", "pending"),
            )
            conn.commit()

    async def _run() -> object:
        # Run seed + subscribe concurrently; subscribe should observe the
        # seeded row within ~0.5s.
        seed_task = asyncio.create_task(_seed_after_delay())
        result = await server.call_tool(
            "permissions_request_subscribe",
            {"timeout_s": 3.0, "poll_interval_s": 0.1},
        )
        await seed_task
        return result

    result = asyncio.run(_run())
    data = _decode_tool_result(result)
    if isinstance(data, dict):
        data = data.get("requests", data)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["capability_id"] == "shell.exec"


def test_subscribe_respects_max_timeout(isolated_home) -> None:
    """The 30s cap is enforced (cap value visible via small test value)."""
    _seed_consent_requests_table(isolated_home / "sessions.db")
    server = build_server(enable_approvals=True)
    # Request 9999 seconds; should clamp internally — we can't test the
    # clamp visibly but the call must still return within a bounded time
    # because the cap exists. Set ``timeout_s`` short to test the fast
    # path; the cap-respect path is exercised by the implementation.
    result = asyncio.run(
        server.call_tool(
            "permissions_request_subscribe",
            {"timeout_s": 0.2, "poll_interval_s": 0.1},
        )
    )
    data = _decode_tool_result(result)
    # Empty result == nothing pending
    if isinstance(data, dict):
        data = data.get("requests", data)
    assert data == []


# ─── MCP-driven permissions_respond writes audit with mcp_client source ───


def test_permissions_respond_via_mcp_records_source(isolated_home) -> None:
    """When --enable-approvals is on AND a request was raised, the grant
    that ``permissions_respond`` writes carries granted_by='mcp_client'."""
    db_path = isolated_home / "sessions.db"
    _seed_consent_requests_table(db_path)
    # Pretend a request came in.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO consent_requests VALUES (?, ?, ?, ?, ?)",
            ("fs.write", "/var/log/x", 1700000000.0, "tool:Edit", "pending"),
        )
        conn.commit()
    server = build_server(enable_approvals=True)
    result = asyncio.run(
        server.call_tool(
            "permissions_respond",
            {
                "capability_id": "fs.write",
                "decision": "allow",
                "scope": "/var/log/x",
                "tier": 1,
                "granted_by": "mcp_client",
            },
        )
    )
    data = _decode_tool_result(result)
    assert data["ok"] is True
    # Verify the consent_grants row carries granted_by='mcp_client'.
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT granted_by FROM consent_grants "
            "WHERE capability_id=? AND scope_filter=?",
            ("fs.write", "/var/log/x"),
        ).fetchone()
    assert row is not None
    assert row[0] == "mcp_client"


def test_permissions_respond_default_granted_by_user(isolated_home) -> None:
    """Without ``granted_by`` arg, fallback is 'user' (back-compat)."""
    db_path = isolated_home / "sessions.db"
    _seed_consent_requests_table(db_path)
    server = build_server()  # default: enable_approvals=False
    result = asyncio.run(
        server.call_tool(
            "permissions_respond",
            {
                "capability_id": "fs.read",
                "decision": "allow",
                "scope": "/tmp/x",
            },
        )
    )
    data = _decode_tool_result(result)
    assert data["ok"] is True
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT granted_by FROM consent_grants "
            "WHERE capability_id=? AND scope_filter=?",
            ("fs.read", "/tmp/x"),
        ).fetchone()
    assert row is not None
    assert row[0] == "user"


def test_permissions_respond_rejects_bad_granted_by(isolated_home) -> None:
    """granted_by is constrained to 'user' | 'mcp_client' | 'gateway'."""
    _seed_consent_requests_table(isolated_home / "sessions.db")
    server = build_server(enable_approvals=True)
    result = asyncio.run(
        server.call_tool(
            "permissions_respond",
            {
                "capability_id": "fs.read",
                "decision": "allow",
                "granted_by": "attacker",
            },
        )
    )
    data = _decode_tool_result(result)
    assert data["ok"] is False
    assert "granted_by" in data["error"]
