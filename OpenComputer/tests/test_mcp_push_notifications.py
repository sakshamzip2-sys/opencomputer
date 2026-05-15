"""Gap H — M3 push notifications via LoggingMessageNotification.

mcp-openclaw-port follow-up. When ``permissions_request_subscribe``
detects a new pending consent request during its long-poll, the server
ALSO emits a ``notifications/message`` (LoggingMessageNotification)
to the client BEFORE returning the long-poll response. This gives
push UX for clients that listen for MCP log notifications.

The push notification payload:

* ``level: "info"``
* ``logger: "openclaw.permission"``
* ``data: {"event": "openclaw.permission.requested", "capability_id": ...,
   "scope": ..., "requested_at": ..., "requested_by": ...}``

External clients filter on ``logger == "openclaw.permission"`` to
receive only OC's consent events without parsing every log message.

Tests use a fake Context whose session captures send_log_message
calls so we can assert the notification was emitted with the right
shape.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.mcp.server import build_server


@pytest.fixture
def isolated_home(tmp_path) -> Generator:
    with patch("opencomputer.mcp.server._home", return_value=tmp_path):
        yield tmp_path


def _seed_consent_requests_table(db_path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS consent_requests ("
            "capability_id TEXT, scope TEXT, requested_at REAL, "
            "requested_by TEXT, state TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS consent_grants ("
            "capability_id TEXT NOT NULL, scope_filter TEXT, "
            "tier INTEGER NOT NULL, granted_at REAL NOT NULL, "
            "expires_at REAL, granted_by TEXT NOT NULL, "
            "PRIMARY KEY (capability_id, scope_filter))"
        )
        conn.commit()


def _decode_tool_result(result):
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


def test_subscribe_emits_log_notification_on_pending_row(
    isolated_home,
) -> None:
    """When subscribe finds a pending row, it sends a log notification
    before returning the long-poll response."""
    db_path = isolated_home / "sessions.db"
    _seed_consent_requests_table(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO consent_requests VALUES (?, ?, ?, ?, ?)",
            ("fs.write", "/etc/passwd", 1700000000.0, "tool:Edit", "pending"),
        )
        conn.commit()

    server = build_server(enable_approvals=True)
    # Locate the tool callable and invoke it with a fake Context.
    # FastMCP's call_tool routes through its own context manager; for a
    # focused test of the push semantics, call the underlying function
    # directly with a mock Context.
    tool_fn = None
    for t in asyncio.run(server.list_tools()):
        if t.name == "permissions_request_subscribe":
            tool_fn = server._tool_manager.get_tool(t.name).fn
            break
    assert tool_fn is not None

    # Build a fake Context whose session captures send_log_message calls.
    fake_session = MagicMock()
    fake_session.send_log_message = AsyncMock()
    fake_ctx = MagicMock()
    fake_ctx.session = fake_session

    result = asyncio.run(tool_fn(
        timeout_s=0.5, poll_interval_s=0.1, ctx=fake_ctx,
    ))

    # The function returns the list of pending requests
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["capability_id"] == "fs.write"

    # And the push notification was emitted at least once with the
    # right shape.
    fake_session.send_log_message.assert_called()
    call_kwargs = fake_session.send_log_message.call_args_list[0].kwargs
    assert call_kwargs["level"] == "info"
    assert call_kwargs["logger"] == "openclaw.permission"
    data = call_kwargs["data"]
    assert data["event"] == "openclaw.permission.requested"
    assert data["capability_id"] == "fs.write"
    assert data["scope"] == "/etc/passwd"


def test_subscribe_no_push_when_no_pending(isolated_home) -> None:
    """Empty consent_requests → timeout → no push notification."""
    _seed_consent_requests_table(isolated_home / "sessions.db")

    server = build_server(enable_approvals=True)
    tool_fn = None
    for t in asyncio.run(server.list_tools()):
        if t.name == "permissions_request_subscribe":
            tool_fn = server._tool_manager.get_tool(t.name).fn
            break
    assert tool_fn is not None

    fake_session = MagicMock()
    fake_session.send_log_message = AsyncMock()
    fake_ctx = MagicMock()
    fake_ctx.session = fake_session

    result = asyncio.run(tool_fn(
        timeout_s=0.2, poll_interval_s=0.1, ctx=fake_ctx,
    ))
    assert result == []
    fake_session.send_log_message.assert_not_called()


def test_subscribe_tool_is_registered_with_context_param(
    isolated_home,
) -> None:
    """The subscribe tool must declare a Context parameter so FastMCP
    auto-injects the session — that's how push notifications get
    routed back to the calling client."""
    server = build_server(enable_approvals=True)
    tool = server._tool_manager.get_tool("permissions_request_subscribe")
    assert tool is not None
    # FastMCP's signature inspection populates fn_metadata; if Context
    # wasn't detected the tool would fail to register.
    import inspect
    sig = inspect.signature(tool.fn)
    has_ctx_param = any(
        "Context" in (str(p.annotation) if p.annotation is not p.empty else "")
        for p in sig.parameters.values()
    )
    assert has_ctx_param, (
        f"expected a Context-annotated parameter, got: {list(sig.parameters.values())}"
    )


def test_subscribe_push_failure_doesnt_block_response(
    isolated_home,
) -> None:
    """If send_log_message raises, the long-poll response still returns."""
    db_path = isolated_home / "sessions.db"
    _seed_consent_requests_table(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO consent_requests VALUES (?, ?, ?, ?, ?)",
            ("fs.read", "/tmp", 1700000000.0, "tool:Read", "pending"),
        )
        conn.commit()

    server = build_server(enable_approvals=True)
    tool_fn = None
    for t in asyncio.run(server.list_tools()):
        if t.name == "permissions_request_subscribe":
            tool_fn = server._tool_manager.get_tool(t.name).fn
            break
    assert tool_fn is not None

    fake_session = MagicMock()

    async def _failing_send(**kwargs: Any) -> None:
        raise RuntimeError("transport down")

    fake_session.send_log_message = _failing_send
    fake_ctx = MagicMock()
    fake_ctx.session = fake_session

    result = asyncio.run(tool_fn(
        timeout_s=0.5, poll_interval_s=0.1, ctx=fake_ctx,
    ))
    # Push failed, response still returns
    assert isinstance(result, list)
    assert len(result) == 1
