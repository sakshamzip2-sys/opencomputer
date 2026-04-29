"""create_realtime_voice_session — direct port of openclaw/src/realtime-voice/session-runtime.ts.

The orchestrator holds an audio sink + tool-call router and calls the
bridge's callbacks. Tests use a FakeBridge to verify the wiring without
opening a real WebSocket.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


class _FakeBridge:
    """Records every method invocation; doesn't actually connect."""

    def __init__(self) -> None:
        self.connected = False
        self.audio_chunks: list[bytes] = []
        self.user_messages: list[str] = []
        self.tool_results: list[tuple[str, Any]] = []
        self.greeting_calls: list[str | None] = []
        self.closed = False
        self._callbacks: dict[str, Any] = {}

    async def connect(self) -> None:
        self.connected = True

    def send_audio(self, audio: bytes) -> None:
        self.audio_chunks.append(audio)

    def send_user_message(self, text: str) -> None:
        self.user_messages.append(text)

    def submit_tool_result(self, call_id: str, result: Any) -> None:
        self.tool_results.append((call_id, result))

    def trigger_greeting(self, instructions: str | None = None) -> None:
        self.greeting_calls.append(instructions)

    def close(self) -> None:
        self.closed = True

    def is_connected(self) -> bool:
        return self.connected and not self.closed


@pytest.mark.asyncio
async def test_session_routes_audio_to_sink() -> None:
    """When the bridge fires onAudio, the audio sink receives it."""
    from opencomputer.voice.realtime_session import create_realtime_voice_session

    bridge = _FakeBridge()
    sink = MagicMock()
    sink.is_open.return_value = True
    sink.send_audio = MagicMock()

    def _create_bridge(callbacks: dict[str, Any]) -> _FakeBridge:
        bridge._callbacks = callbacks
        return bridge

    session = create_realtime_voice_session(
        create_bridge=_create_bridge,
        audio_sink=sink,
    )
    await session.connect()

    bridge._callbacks["on_audio"](b"\x00\x01\x02\x03")
    sink.send_audio.assert_called_once_with(b"\x00\x01\x02\x03")


@pytest.mark.asyncio
async def test_session_routes_tool_calls_to_router() -> None:
    """When the bridge fires onToolCall, the session forwards to the
    user-supplied router and the router's result is pushed back."""
    from opencomputer.voice.realtime_session import create_realtime_voice_session
    from plugin_sdk.realtime_voice import RealtimeVoiceToolCallEvent

    bridge = _FakeBridge()
    received_calls: list[RealtimeVoiceToolCallEvent] = []

    def _router(event: RealtimeVoiceToolCallEvent, sess: Any) -> None:
        received_calls.append(event)
        sess.submit_tool_result(event.call_id, {"output": "ok"})

    def _create_bridge(callbacks: dict[str, Any]) -> _FakeBridge:
        bridge._callbacks = callbacks
        return bridge

    session = create_realtime_voice_session(
        create_bridge=_create_bridge,
        audio_sink=MagicMock(is_open=MagicMock(return_value=True)),
        on_tool_call=_router,
    )
    await session.connect()

    ev = RealtimeVoiceToolCallEvent(
        item_id="i1", call_id="c1", name="Bash", args={"command": "ls"},
    )
    bridge._callbacks["on_tool_call"](ev)
    assert received_calls == [ev]
    assert bridge.tool_results == [("c1", {"output": "ok"})]


@pytest.mark.asyncio
async def test_session_skips_audio_when_sink_closed() -> None:
    """If the audio sink reports is_open=False, drop incoming audio."""
    from opencomputer.voice.realtime_session import create_realtime_voice_session

    bridge = _FakeBridge()
    sink = MagicMock()
    sink.is_open.return_value = False
    sink.send_audio = MagicMock()

    def _create_bridge(callbacks: dict[str, Any]) -> _FakeBridge:
        bridge._callbacks = callbacks
        return bridge

    session = create_realtime_voice_session(
        create_bridge=_create_bridge,
        audio_sink=sink,
    )
    await session.connect()
    bridge._callbacks["on_audio"](b"hello")
    sink.send_audio.assert_not_called()


@pytest.mark.asyncio
async def test_session_clear_audio_calls_sink_clear() -> None:
    """Barge-in: bridge fires onClearAudio, sink.clear_audio() runs."""
    from opencomputer.voice.realtime_session import create_realtime_voice_session

    bridge = _FakeBridge()
    sink = MagicMock()
    sink.is_open.return_value = True
    sink.clear_audio = MagicMock()

    def _create_bridge(callbacks: dict[str, Any]) -> _FakeBridge:
        bridge._callbacks = callbacks
        return bridge

    session = create_realtime_voice_session(
        create_bridge=_create_bridge,
        audio_sink=sink,
    )
    await session.connect()
    bridge._callbacks["on_clear_audio"]()
    sink.clear_audio.assert_called_once()


@pytest.mark.asyncio
async def test_session_trigger_greeting_on_ready_when_enabled() -> None:
    from opencomputer.voice.realtime_session import create_realtime_voice_session

    bridge = _FakeBridge()
    bridge.trigger_greeting = MagicMock()  # type: ignore[method-assign]

    def _create_bridge(callbacks: dict[str, Any]) -> _FakeBridge:
        bridge._callbacks = callbacks
        return bridge

    session = create_realtime_voice_session(
        create_bridge=_create_bridge,
        audio_sink=MagicMock(is_open=MagicMock(return_value=True)),
        trigger_greeting_on_ready=True,
        initial_greeting_instructions="say hi",
    )
    await session.connect()
    bridge._callbacks["on_ready"]()
    bridge.trigger_greeting.assert_called_once_with("say hi")
