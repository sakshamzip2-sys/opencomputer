"""Phase 9 tests: WebSocket wire server + protocol dispatch."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets


async def _find_free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _build_fake_loop():
    """Build a lightweight AgentLoop mock with the attributes WireServer touches."""
    from opencomputer.agent.loop import ConversationResult
    from plugin_sdk.core import Message

    loop = MagicMock()
    # db — used by sessions.list + search
    loop.db.list_sessions = MagicMock(
        return_value=[
            {"id": "s1", "started_at": 1.0, "message_count": 3},
        ]
    )
    loop.db.search = MagicMock(
        return_value=[{"session_id": "s1", "role": "user", "snippet": "test"}]
    )
    # memory — used by skills.list
    from opencomputer.agent.memory import SkillMeta
    from pathlib import Path

    loop.memory.list_skills = MagicMock(
        return_value=[
            SkillMeta(id="skill-1", name="Skill One", description="desc", path=Path("/tmp/x"), version="0.1.0")
        ]
    )

    # run_conversation — used by chat
    final = Message(role="assistant", content="hello from agent")
    result = ConversationResult(
        final_message=final,
        messages=[final],
        session_id="test-session",
        iterations=1,
        input_tokens=10,
        output_tokens=5,
    )
    loop.run_conversation = AsyncMock(return_value=result)
    return loop


# ─── WireServer lifecycle ───────────────────────────────────────


def test_wire_server_start_stop() -> None:
    """Server binds and unbinds cleanly."""
    from opencomputer.gateway.wire_server import WireServer

    async def run():
        port = await _find_free_port()
        server = WireServer(loop=_build_fake_loop(), port=port)
        await server.start()
        await server.stop()

    asyncio.run(run())


def test_wire_hello_handshake_returns_capabilities() -> None:
    from opencomputer.gateway.wire_server import WireServer

    async def run():
        port = await _find_free_port()
        server = WireServer(loop=_build_fake_loop(), port=port)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(json.dumps({"type": "req", "id": "1", "method": "hello"}))
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                data = json.loads(raw)
                assert data["type"] == "res"
                assert data["id"] == "1"
                assert data["ok"] is True
                assert data["payload"]["server"] == "opencomputer"
                assert "chat" in data["payload"]["methods"]
                assert "turn.begin" in data["payload"]["events"]
        finally:
            await server.stop()

    asyncio.run(run())


def test_wire_sessions_list_dispatch() -> None:
    from opencomputer.gateway.wire_server import WireServer

    fake_loop = _build_fake_loop()

    async def run():
        port = await _find_free_port()
        server = WireServer(loop=fake_loop, port=port)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "req",
                            "id": "2",
                            "method": "sessions.list",
                            "params": {"limit": 5},
                        }
                    )
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                data = json.loads(raw)
                assert data["ok"] is True
                assert len(data["payload"]["sessions"]) == 1
                assert data["payload"]["sessions"][0]["id"] == "s1"
        finally:
            await server.stop()

    asyncio.run(run())


def test_wire_unknown_method_errors_cleanly() -> None:
    from opencomputer.gateway.wire_server import WireServer

    async def run():
        port = await _find_free_port()
        server = WireServer(loop=_build_fake_loop(), port=port)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "req",
                            "id": "99",
                            "method": "nope.does.not.exist",
                        }
                    )
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                data = json.loads(raw)
                assert data["ok"] is False
                assert "unknown method" in (data["error"] or "").lower()
        finally:
            await server.stop()

    asyncio.run(run())


def test_wire_invalid_json_errors_cleanly() -> None:
    from opencomputer.gateway.wire_server import WireServer

    async def run():
        port = await _find_free_port()
        server = WireServer(loop=_build_fake_loop(), port=port)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send("not valid json {{{{")
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                data = json.loads(raw)
                assert data["ok"] is False
                assert "json" in (data["error"] or "").lower()
        finally:
            await server.stop()

    asyncio.run(run())


def test_wire_chat_emits_turn_events_and_final_response() -> None:
    """chat method: emits turn.begin, assistant.message (if any), turn.end, then final response."""
    from opencomputer.gateway.wire_server import WireServer

    fake_loop = _build_fake_loop()

    async def run():
        port = await _find_free_port()
        server = WireServer(loop=fake_loop, port=port)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "req",
                            "id": "c1",
                            "method": "chat",
                            "params": {"message": "hi"},
                        }
                    )
                )
                # Collect messages until we get the terminal response
                events = []
                response = None
                for _ in range(10):
                    raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    msg = json.loads(raw)
                    if msg.get("type") == "event":
                        events.append(msg["event"])
                    elif msg.get("type") == "res":
                        response = msg
                        break
                assert response is not None
                assert response["ok"] is True
                assert response["payload"]["text"] == "hello from agent"
                assert "turn.begin" in events
                assert "turn.end" in events
        finally:
            await server.stop()

    asyncio.run(run())


def test_wire_chat_empty_message_rejected() -> None:
    from opencomputer.gateway.wire_server import WireServer

    async def run():
        port = await _find_free_port()
        server = WireServer(loop=_build_fake_loop(), port=port)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "req",
                            "id": "c2",
                            "method": "chat",
                            "params": {"message": "   "},
                        }
                    )
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                data = json.loads(raw)
                assert data["ok"] is False
                assert "empty" in (data["error"] or "").lower()
        finally:
            await server.stop()

    asyncio.run(run())


# ─── CLI has the wire subcommand ────────────────────────────────


def test_cli_exposes_wire_command() -> None:
    """The opencomputer CLI should have a 'wire' subcommand."""
    from opencomputer.cli import app

    names = []
    for cmd in app.registered_commands:
        names.append(cmd.name or getattr(cmd.callback, "__name__", ""))
    assert "wire" in names
