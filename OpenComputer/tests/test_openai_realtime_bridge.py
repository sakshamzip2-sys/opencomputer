"""OpenAI Realtime bridge — port of openclaw/extensions/openai/realtime-voice-provider.ts.

Tests use a fake WebSocket exposing ``send`` (records frames) and a
configurable inbound queue.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
from unittest.mock import MagicMock

import pytest


class _FakeWS:
    """Records every outbound send + lets tests push inbound frames."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._inbound: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        msg = await self._inbound.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    async def close(self) -> None:
        self.closed = True
        await self._inbound.put(None)

    def push(self, payload: dict[str, Any]) -> None:
        self._inbound.put_nowait(json.dumps(payload))


def _make_bridge(callbacks: dict[str, Any] | None = None) -> Any:
    from extensions.openai_provider.realtime import OpenAIRealtimeBridge

    cb = callbacks or {}
    return OpenAIRealtimeBridge(
        api_key="sk-test",
        model="gpt-realtime-1.5",
        voice="alloy",
        instructions="be helpful",
        tools=(),
        on_audio=cb.get("on_audio") or (lambda b: None),
        on_clear_audio=cb.get("on_clear_audio") or (lambda: None),
        on_transcript=cb.get("on_transcript"),
        on_tool_call=cb.get("on_tool_call"),
        on_ready=cb.get("on_ready"),
        on_error=cb.get("on_error"),
        on_close=cb.get("on_close"),
    )


@pytest.mark.asyncio
async def test_session_update_sent_on_open() -> None:
    """When the WS opens, the bridge sends a session.update with PCM16."""
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    b = _make_bridge()
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"type": "session.created"})
    fake_ws.push({"type": "session.updated"})
    await asyncio.sleep(0.05)
    b.close()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        pass

    assert fake_ws.sent, "no frames sent"
    first = json.loads(fake_ws.sent[0])
    assert first["type"] == "session.update"
    assert first["session"]["input_audio_format"] == "pcm16"
    assert first["session"]["output_audio_format"] == "pcm16"
    assert first["session"]["voice"] == "alloy"


@pytest.mark.asyncio
async def test_audio_delta_calls_on_audio() -> None:
    """response.audio.delta with base64 PCM16 → on_audio(bytes)."""
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    audio_chunks: list[bytes] = []
    b = _make_bridge({"on_audio": audio_chunks.append})
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"type": "session.created"})
    fake_ws.push({"type": "session.updated"})

    payload = base64.b64encode(b"\x10\x20\x30\x40").decode()
    fake_ws.push({"type": "response.audio.delta", "delta": payload, "item_id": "i1"})
    await asyncio.sleep(0.05)
    b.close()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        pass

    assert audio_chunks == [b"\x10\x20\x30\x40"]


@pytest.mark.asyncio
async def test_speech_started_triggers_on_clear_audio() -> None:
    """Server VAD sees user speak → barge-in → on_clear_audio fires."""
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    cleared = MagicMock()
    b = _make_bridge({"on_clear_audio": cleared})
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"type": "session.created"})
    fake_ws.push({"type": "session.updated"})
    fake_ws.push({"type": "input_audio_buffer.speech_started"})
    await asyncio.sleep(0.05)
    b.close()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        pass
    cleared.assert_called()


@pytest.mark.asyncio
async def test_tool_call_arguments_buffered_and_dispatched() -> None:
    """Function call deltas accumulate; .done emits one ToolCallEvent."""
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    received: list[Any] = []
    b = _make_bridge({"on_tool_call": received.append})
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"type": "session.created"})
    fake_ws.push({"type": "session.updated"})
    fake_ws.push({
        "type": "response.function_call_arguments.delta",
        "item_id": "item_1",
        "call_id": "call_x",
        "name": "Bash",
        "delta": '{"command":"',
    })
    fake_ws.push({
        "type": "response.function_call_arguments.delta",
        "item_id": "item_1",
        "delta": 'ls -la"}',
    })
    fake_ws.push({
        "type": "response.function_call_arguments.done",
        "item_id": "item_1",
    })
    await asyncio.sleep(0.05)
    b.close()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        pass

    assert len(received) == 1
    ev = received[0]
    assert ev.call_id == "call_x"
    assert ev.name == "Bash"
    assert ev.args == {"command": "ls -la"}


@pytest.mark.asyncio
async def test_send_audio_appends_to_input_buffer() -> None:
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    b = _make_bridge()
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"type": "session.created"})
    fake_ws.push({"type": "session.updated"})
    await asyncio.sleep(0.05)

    b.send_audio(b"\x01\x02\x03")
    await asyncio.sleep(0.05)
    b.close()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        pass

    audio_frames = [
        json.loads(s) for s in fake_ws.sent
        if json.loads(s).get("type") == "input_audio_buffer.append"
    ]
    assert len(audio_frames) == 1
    assert base64.b64decode(audio_frames[0]["audio"]) == b"\x01\x02\x03"


@pytest.mark.asyncio
async def test_submit_tool_result_creates_function_call_output() -> None:
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    b = _make_bridge()
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"type": "session.created"})
    fake_ws.push({"type": "session.updated"})
    await asyncio.sleep(0.05)

    b.submit_tool_result("call_x", {"output": "hello"})
    await asyncio.sleep(0.05)
    b.close()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        pass

    creates = [
        json.loads(s) for s in fake_ws.sent
        if json.loads(s).get("type") == "conversation.item.create"
    ]
    assert any(
        c["item"]["type"] == "function_call_output"
        and c["item"]["call_id"] == "call_x"
        and json.loads(c["item"]["output"]) == {"output": "hello"}
        for c in creates
    )
    response_creates = [
        json.loads(s) for s in fake_ws.sent
        if json.loads(s).get("type") == "response.create"
    ]
    assert response_creates  # bridge triggers a new response after tool result
