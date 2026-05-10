"""T64 — outbound `session/requestPermission` to the IDE.

Direction: agent → IDE. The agent decides it needs user approval, sends
a JSON-RPC request, awaits the IDE's response, returns the verdict.

Pre-existing inbound `requestPermission` handler (IDE → agent) is a
different, complementary surface and is unchanged by T64.
"""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.acp.server import ACPServer


@pytest.fixture
def server_with_capture():
    server = ACPServer()
    captured: list[dict] = []
    server._write = lambda msg: captured.append(msg)
    return server, captured


@pytest.mark.asyncio
async def test_request_permission_emits_outbound_jsonrpc(server_with_capture):
    """Confirms the outbound RPC has a request id and the right method."""
    server, captured = server_with_capture

    async def runner():
        return await server.request_permission(
            session_id="s1",
            command="Bash:rm -rf /",
            description="Delete everything",
            timeout=2.0,
        )

    task = asyncio.create_task(runner())
    # Yield so the request is written before we inspect.
    await asyncio.sleep(0)
    requests = [m for m in captured if m.get("method") == "session/requestPermission"]
    assert len(requests) == 1
    req = requests[0]
    assert "id" in req  # a sync RPC call (request, not notification)
    assert req["params"]["sessionId"] == "s1"
    assert req["params"]["command"] == "Bash:rm -rf /"
    assert req["params"]["description"] == "Delete everything"

    # Simulate IDE responding allow.
    response = {
        "jsonrpc": "2.0",
        "id": req["id"],
        "result": {"outcome": "allow", "grantType": "once"},
    }
    await server._dispatch(response)
    verdict = await asyncio.wait_for(task, timeout=2.0)
    assert verdict["outcome"] == "allow"


@pytest.mark.asyncio
async def test_request_permission_handles_deny(server_with_capture):
    server, captured = server_with_capture

    async def runner():
        return await server.request_permission(
            session_id="s2",
            command="Edit:secret.env",
            description="Modify secret",
            timeout=2.0,
        )

    task = asyncio.create_task(runner())
    await asyncio.sleep(0)
    req = next(m for m in captured if m.get("method") == "session/requestPermission")
    response = {
        "jsonrpc": "2.0",
        "id": req["id"],
        "result": {"outcome": "deny"},
    }
    await server._dispatch(response)
    verdict = await asyncio.wait_for(task, timeout=2.0)
    assert verdict["outcome"] == "deny"


@pytest.mark.asyncio
async def test_request_permission_timeout(server_with_capture):
    """No IDE response within timeout → auto-deny."""
    server, _ = server_with_capture

    verdict = await server.request_permission(
        session_id="s3",
        command="X",
        description="",
        timeout=0.05,
    )
    assert verdict["outcome"] == "deny"
    assert verdict["reason"] == "timeout"


@pytest.mark.asyncio
async def test_response_for_unknown_id_silently_ignored(server_with_capture):
    """Stale or duplicate IDE responses must not crash the dispatcher."""
    server, _ = server_with_capture
    # No outstanding request — this id is bogus.
    await server._dispatch(
        {"jsonrpc": "2.0", "id": "bogus-id-99", "result": {"outcome": "allow"}}
    )
    # Implicit assertion: no exception bubbles out.


@pytest.mark.asyncio
async def test_request_permission_error_response(server_with_capture):
    """If the IDE returns a JSON-RPC error, surface it as deny + reason."""
    server, captured = server_with_capture

    async def runner():
        return await server.request_permission(
            session_id="s4",
            command="X",
            description="",
            timeout=2.0,
        )

    task = asyncio.create_task(runner())
    await asyncio.sleep(0)
    req = next(m for m in captured if m.get("method") == "session/requestPermission")
    await server._dispatch(
        {
            "jsonrpc": "2.0",
            "id": req["id"],
            "error": {"code": -32601, "message": "method not found"},
        }
    )
    verdict = await asyncio.wait_for(task, timeout=2.0)
    assert verdict["outcome"] == "deny"
    assert "method not found" in verdict["reason"]
