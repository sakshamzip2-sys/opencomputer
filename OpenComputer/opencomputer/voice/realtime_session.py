"""Session orchestration for realtime voice.

Direct Python port of openclaw/src/realtime-voice/session-runtime.ts (commit 2026-04-23).
The function ``create_realtime_voice_session`` builds a session by calling
the user-supplied ``create_bridge`` factory with a callbacks dict, then
wires the callbacks to: an audio sink (mic/speaker), an optional
tool-call router, and optional ready/error/close hooks.

Mark protocol — the TS version supports a Twilio-style mark/ack protocol
for telephony synchronization. Local mic/speaker doesn't need it; we
omit the entire surface.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from plugin_sdk.realtime_voice import (
    BaseRealtimeVoiceBridge,
    RealtimeVoiceCloseReason,
    RealtimeVoiceRole,
    RealtimeVoiceToolCallEvent,
)


class RealtimeVoiceAudioSink(Protocol):
    """What the session expects from a sink — minimal protocol."""

    def is_open(self) -> bool: ...
    def send_audio(self, audio: bytes) -> None: ...
    def clear_audio(self) -> None: ...  # called on barge-in


@dataclass
class RealtimeVoiceSession:
    """Wraps a bridge with the session orchestration glue.

    Returned by ``create_realtime_voice_session``. The agent loop calls
    ``connect``, then forwards user audio via ``send_audio`` (or text via
    ``send_user_message``), and disposes via ``close``.
    """

    bridge: BaseRealtimeVoiceBridge

    async def connect(self) -> None:
        await self.bridge.connect()

    def send_audio(self, audio: bytes) -> None:
        self.bridge.send_audio(audio)

    def send_user_message(self, text: str) -> None:
        self.bridge.send_user_message(text)

    def submit_tool_result(self, call_id: str, result: Any) -> None:
        self.bridge.submit_tool_result(call_id, result)

    def trigger_greeting(self, instructions: str | None = None) -> None:
        self.bridge.trigger_greeting(instructions)

    def close(self) -> None:
        self.bridge.close()


def create_realtime_voice_session(
    *,
    create_bridge: Callable[[dict[str, Any]], BaseRealtimeVoiceBridge],
    audio_sink: RealtimeVoiceAudioSink,
    on_transcript: Callable[[RealtimeVoiceRole, str, bool], None] | None = None,
    on_tool_call: (
        Callable[[RealtimeVoiceToolCallEvent, RealtimeVoiceSession], None] | None
    ) = None,
    on_ready: Callable[[RealtimeVoiceSession], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
    on_close: Callable[[RealtimeVoiceCloseReason], None] | None = None,
    trigger_greeting_on_ready: bool = False,
    initial_greeting_instructions: str | None = None,
) -> RealtimeVoiceSession:
    """Create + return a :class:`RealtimeVoiceSession`.

    ``create_bridge`` receives a callbacks dict (on_audio, on_clear_audio,
    on_transcript, on_tool_call, on_ready, on_error, on_close) and must
    return a concrete bridge instance wired to those callbacks. We pass
    callbacks by dict so the bridge ABC stays agnostic of how callbacks
    are stored — TS uses an options object; Python lets us dict-spread.
    """
    bridge: BaseRealtimeVoiceBridge | None = None
    session: RealtimeVoiceSession  # forward ref filled in below

    def _can_send_audio() -> bool:
        try:
            return bool(audio_sink.is_open())
        except AttributeError:
            return True

    def _on_audio(audio: bytes) -> None:
        if _can_send_audio():
            audio_sink.send_audio(audio)

    def _on_clear_audio() -> None:
        if _can_send_audio():
            try:
                audio_sink.clear_audio()
            except AttributeError:
                pass  # sink doesn't support barge-in — no-op is fine

    def _on_tool_call(event: RealtimeVoiceToolCallEvent) -> None:
        if on_tool_call is not None and bridge is not None:
            on_tool_call(event, session)

    def _on_ready() -> None:
        if bridge is None:
            return
        if trigger_greeting_on_ready:
            bridge.trigger_greeting(initial_greeting_instructions)
        if on_ready is not None:
            on_ready(session)

    callbacks: dict[str, Any] = {
        "on_audio": _on_audio,
        "on_clear_audio": _on_clear_audio,
        "on_transcript": on_transcript,
        "on_tool_call": _on_tool_call,
        "on_ready": _on_ready,
        "on_error": on_error,
        "on_close": on_close,
    }
    bridge = create_bridge(callbacks)
    session = RealtimeVoiceSession(bridge=bridge)
    return session


__all__ = [
    "RealtimeVoiceAudioSink",
    "RealtimeVoiceSession",
    "create_realtime_voice_session",
]
