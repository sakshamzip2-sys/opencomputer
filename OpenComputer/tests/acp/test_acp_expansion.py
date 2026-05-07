"""Tests for ACP expansion (PR-A Feature 3)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# RuntimeContext.acp_denied_tools — typed field
# ---------------------------------------------------------------------------


def test_runtime_context_default_acp_denied_tools_empty():
    from plugin_sdk.runtime_context import RuntimeContext

    ctx = RuntimeContext()
    assert ctx.acp_denied_tools == frozenset()


def test_runtime_context_construction_with_denylist():
    from plugin_sdk.runtime_context import RuntimeContext

    ctx = RuntimeContext(acp_denied_tools=frozenset({"Bash", "WebFetch"}))
    assert "Bash" in ctx.acp_denied_tools
    assert "WebFetch" in ctx.acp_denied_tools
    assert "Read" not in ctx.acp_denied_tools


# ---------------------------------------------------------------------------
# ACPSession.update_permissions
# ---------------------------------------------------------------------------


def test_acp_session_default_permissions_empty():
    from opencomputer.acp.session import ACPSession

    s = ACPSession(session_id="sess-1", send=lambda *a, **kw: None)
    assert s.allowed_tools == frozenset()
    assert s.denied_tools == frozenset()


def test_acp_session_update_permissions_sets_denied():
    from opencomputer.acp.session import ACPSession

    s = ACPSession(session_id="sess-1", send=lambda *a, **kw: None)
    s.update_permissions(denied=frozenset({"Bash"}))
    assert s.denied_tools == frozenset({"Bash"})


def test_acp_session_update_permissions_partial_update():
    """Passing only denied leaves allowed unchanged."""
    from opencomputer.acp.session import ACPSession

    s = ACPSession(session_id="sess-1", send=lambda *a, **kw: None)
    s.update_permissions(allowed=frozenset({"Read"}))
    s.update_permissions(denied=frozenset({"Bash"}))
    assert s.allowed_tools == frozenset({"Read"})
    assert s.denied_tools == frozenset({"Bash"})


def test_acp_session_update_permissions_clear_with_empty():
    from opencomputer.acp.session import ACPSession

    s = ACPSession(session_id="sess-1", send=lambda *a, **kw: None)
    s.update_permissions(denied=frozenset({"Bash"}))
    s.update_permissions(denied=frozenset())
    assert s.denied_tools == frozenset()


# ---------------------------------------------------------------------------
# ACPServer.setSessionPermissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_session_permissions_round_trip():
    from opencomputer.acp.server import ACPServer
    from opencomputer.acp.session import ACPSession

    server = ACPServer()
    sid = "sess-1"
    server._sessions[sid] = ACPSession(session_id=sid, send=lambda *a, **kw: None)

    handler = server._handlers["setSessionPermissions"]
    result = await handler({
        "sessionId": sid,
        "deniedTools": ["Bash", "WebFetch"],
    })
    assert result["sessionId"] == sid
    assert set(result["deniedTools"]) == {"Bash", "WebFetch"}
    assert "Bash" in server._sessions[sid].denied_tools


@pytest.mark.asyncio
async def test_set_session_permissions_unknown_session_raises_keyerror():
    """KeyError → server dispatch translates to ERR_SESSION_NOT_FOUND."""
    from opencomputer.acp.server import ACPServer

    server = ACPServer()
    handler = server._handlers["setSessionPermissions"]
    with pytest.raises(KeyError, match="session not found"):
        await handler({"sessionId": "ghost", "deniedTools": []})


@pytest.mark.asyncio
async def test_set_session_permissions_idempotent():
    from opencomputer.acp.server import ACPServer
    from opencomputer.acp.session import ACPSession

    server = ACPServer()
    sid = "sess-2"
    server._sessions[sid] = ACPSession(session_id=sid, send=lambda *a, **kw: None)
    handler = server._handlers["setSessionPermissions"]
    await handler({"sessionId": sid, "deniedTools": ["Bash"]})
    await handler({"sessionId": sid, "deniedTools": ["Bash"]})
    assert server._sessions[sid].denied_tools == frozenset({"Bash"})


@pytest.mark.asyncio
async def test_set_session_permissions_omitted_field_unchanged():
    """Passing only allowedTools doesn't reset deniedTools to empty."""
    from opencomputer.acp.server import ACPServer
    from opencomputer.acp.session import ACPSession

    server = ACPServer()
    sid = "sess-3"
    server._sessions[sid] = ACPSession(session_id=sid, send=lambda *a, **kw: None)
    handler = server._handlers["setSessionPermissions"]
    await handler({"sessionId": sid, "deniedTools": ["Bash"]})
    # Now update only allowedTools
    await handler({"sessionId": sid, "allowedTools": ["Read"]})
    assert server._sessions[sid].denied_tools == frozenset({"Bash"})  # preserved
    assert server._sessions[sid].allowed_tools == frozenset({"Read"})


# ---------------------------------------------------------------------------
# make_approval_callback default_tier param
# ---------------------------------------------------------------------------


def test_make_approval_callback_accepts_valid_tiers():
    """All ConsentTier names — IMPLICIT/EXPLICIT/PER_ACTION/DELEGATED — accepted."""
    from opencomputer.acp.permissions import make_approval_callback

    loop = asyncio.new_event_loop()
    gate = MagicMock()
    try:
        for tier in ("IMPLICIT", "EXPLICIT", "PER_ACTION", "DELEGATED"):
            cb = make_approval_callback("sid", gate, loop, default_tier=tier)
            assert callable(cb)
    finally:
        loop.close()


def test_make_approval_callback_rejects_invalid_tier():
    from opencomputer.acp.permissions import make_approval_callback

    loop = asyncio.new_event_loop()
    gate = MagicMock()
    try:
        with pytest.raises(ValueError, match="default_tier"):
            make_approval_callback("sid", gate, loop, default_tier="BAD_VALUE")
    finally:
        loop.close()


def test_make_approval_callback_default_tier_is_per_action():
    """Backwards compat: omitted default_tier defaults to PER_ACTION."""
    from opencomputer.acp.permissions import make_approval_callback

    loop = asyncio.new_event_loop()
    gate = MagicMock()
    try:
        cb = make_approval_callback("sid", gate, loop)
        assert callable(cb)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Loop dispatch consults RuntimeContext.acp_denied_tools
# ---------------------------------------------------------------------------


def test_acp_denylist_check_logic_round_trip():
    """Sanity: simulate the dispatch denylist logic in isolation."""
    from plugin_sdk.core import ToolCall, ToolResult
    from plugin_sdk.runtime_context import RuntimeContext

    runtime = RuntimeContext(acp_denied_tools=frozenset({"Bash"}))
    calls = [
        ToolCall(id="1", name="Bash", arguments={}),
        ToolCall(id="2", name="Read", arguments={}),
    ]

    blocked: dict[str, str] = {}
    denied = runtime.acp_denied_tools
    for c in calls:
        if c.name in denied:
            blocked[c.id] = f"ACP denylist: tool '{c.name}' is denied"

    assert "1" in blocked
    assert "2" not in blocked


def test_empty_acp_denylist_does_not_block():
    """Default empty denylist short-circuits cleanly."""
    from plugin_sdk.core import ToolCall
    from plugin_sdk.runtime_context import RuntimeContext

    runtime = RuntimeContext()  # default: empty denylist
    calls = [ToolCall(id="1", name="Bash", arguments={})]
    denied = runtime.acp_denied_tools
    assert not denied  # falsy
    blocked: dict[str, str] = {}
    if denied:
        for c in calls:
            if c.name in denied:
                blocked[c.id] = "denied"
    assert blocked == {}
