"""PR-D: ACP server tests — JSON-RPC handlers + session lifecycle."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.acp.server import (
    ACP_PROTOCOL_VERSION,
    ACP_SERVER_NAME,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_SESSION_NOT_FOUND,
    ACPServer,
)


@pytest.fixture
def server_with_capture():
    """Server whose _write captures messages instead of writing to stdout."""
    server = ACPServer()
    captured: list[dict] = []
    server._write = lambda msg: captured.append(msg)
    return server, captured


@pytest.mark.asyncio
async def test_uninitialized_other_method_returns_error(server_with_capture):
    server, captured = server_with_capture
    await server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "newSession"})
    assert len(captured) == 1
    assert captured[0]["error"]["code"] == ERR_INVALID_REQUEST
    assert "not initialized" in captured[0]["error"]["message"]


@pytest.mark.asyncio
async def test_initialize_returns_capabilities(server_with_capture):
    server, captured = server_with_capture
    await server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert len(captured) == 1
    result = captured[0]["result"]
    assert result["protocolVersion"] == ACP_PROTOCOL_VERSION
    # serverInfo shape (updated in ACP depth uplift)
    assert result["serverInfo"]["name"] == ACP_SERVER_NAME
    caps = result["serverCapabilities"]
    assert caps["streaming"] is True
    assert caps["cancellation"] is True
    assert "provider" in caps


@pytest.mark.asyncio
async def test_new_session_returns_session_id(server_with_capture):
    server, captured = server_with_capture
    await server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "newSession", "params": {}})
    assert len(captured) == 2
    sid = captured[1]["result"]["sessionId"]
    assert sid.startswith("acp:")


@pytest.mark.asyncio
async def test_new_session_with_meta_session_key_uses_override(server_with_capture):
    server, captured = server_with_capture
    await server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await server._dispatch({
        "jsonrpc": "2.0", "id": 2, "method": "newSession",
        "params": {"_meta": {"sessionKey": "my-custom-key"}},
    })
    assert captured[1]["result"]["sessionId"] == "my-custom-key"


@pytest.mark.asyncio
async def test_unknown_method_returns_error(server_with_capture):
    server, captured = server_with_capture
    await server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "totally_made_up"})
    assert captured[1]["error"]["code"] == ERR_METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_prompt_for_nonexistent_session_returns_session_not_found(server_with_capture):
    server, captured = server_with_capture
    await server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await server._dispatch({
        "jsonrpc": "2.0", "id": 2, "method": "prompt",
        "params": {"sessionId": "nope", "content": "hi"},
    })
    assert captured[1]["error"]["code"] == ERR_SESSION_NOT_FOUND


@pytest.mark.asyncio
async def test_list_sessions_after_new_session(server_with_capture):
    server, captured = server_with_capture
    await server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "newSession", "params": {}})
    await server._dispatch({"jsonrpc": "2.0", "id": 3, "method": "listSessions", "params": {}})
    assert len(captured) == 3
    sessions = captured[2]["result"]["sessions"]
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_cancel_for_nonexistent_session_returns_not_found(server_with_capture):
    server, captured = server_with_capture
    await server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await server._dispatch({
        "jsonrpc": "2.0", "id": 2, "method": "cancel",
        "params": {"sessionId": "nope"},
    })
    assert captured[1]["error"]["code"] == ERR_SESSION_NOT_FOUND


@pytest.mark.asyncio
async def test_load_session_unknown_returns_session_not_found(server_with_capture):
    server, captured = server_with_capture
    await server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await server._dispatch({
        "jsonrpc": "2.0", "id": 2, "method": "loadSession",
        "params": {"sessionId": "does-not-exist-anywhere"},
    })
    assert captured[1]["error"]["code"] == ERR_SESSION_NOT_FOUND


@pytest.mark.asyncio
async def test_prompt_with_empty_content_rejected(server_with_capture):
    server, captured = server_with_capture
    await server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "newSession", "params": {}})
    sid = captured[1]["result"]["sessionId"]
    await server._dispatch({
        "jsonrpc": "2.0", "id": 3, "method": "prompt",
        "params": {"sessionId": sid, "content": "   "},
    })
    assert captured[2]["error"]["code"] != 0  # some error code


def test_acp_module_exports():
    """Public API surface."""
    from opencomputer.acp import ACPServer, ACPSession
    assert ACPServer is not None
    assert ACPSession is not None


def test_detect_provider_returns_string_or_none():
    from opencomputer.acp.auth import detect_provider, has_provider
    result = detect_provider()
    assert result is None or isinstance(result, str)
    assert isinstance(has_provider(), bool)


@pytest.mark.asyncio
async def test_initialize_includes_provider_in_capabilities():
    """initialize response should include serverCapabilities.provider."""
    server = ACPServer()
    result = await server._handle_initialize({"clientCapabilities": {}})
    caps = result.get("serverCapabilities", {})
    assert "provider" in caps


@pytest.mark.asyncio
async def test_request_permission_method_registered():
    """ACPServer should handle 'requestPermission' method."""
    server = ACPServer()
    assert "requestPermission" in server._handlers
