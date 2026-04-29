"""OpenAI Realtime WebSocket bridge.

Direct Python port of openclaw/extensions/openai/realtime-voice-provider.ts (commit 2026-04-23).
Differences from the TS original:

* PCM16 audio format (``pcm16``) instead of g711_ulaw — local mic/speaker
  use 16 kHz signed-16 raw PCM, telephony's μ-law isn't relevant.
* Mark protocol (markQueue/sendMark/acknowledgeMark) is dropped — those
  exist for Twilio Media Streams synchronization, not local audio.
* Proxy-capture and capture-WS-event hooks are dropped — OC has its own
  observability (logging_config + journald handlers).
* Reconnect behavior preserved: 5 attempts with exponential backoff
  (1s, 2s, 4s, 8s, 16s).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import websockets
from extensions.openai_provider.realtime_helpers import (
    read_realtime_error_detail,
)

from plugin_sdk.realtime_voice import (
    BaseRealtimeVoiceBridge,
    RealtimeVoiceCloseReason,
    RealtimeVoiceRole,
    RealtimeVoiceTool,
    RealtimeVoiceToolCallEvent,
)

_log = logging.getLogger("opencomputer.providers.openai.realtime")

_DEFAULT_MODEL = "gpt-realtime-1.5"
_MAX_RECONNECT_ATTEMPTS = 5
_BASE_RECONNECT_DELAY_S = 1.0
_CONNECT_TIMEOUT_S = 10.0
_PENDING_AUDIO_CAP = 320  # frames buffered before session is ready


class OpenAIRealtimeBridge(BaseRealtimeVoiceBridge):
    """Concrete realtime bridge for OpenAI's wss://api.openai.com/v1/realtime.

    The WS handle is type-annotated ``Any`` rather than
    ``WebSocketClientProtocol`` because the latter import is deprecated in
    websockets >=15 (audit B4). Internal-only.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None = None,
        voice: str = "alloy",
        instructions: str | None = None,
        tools: tuple[RealtimeVoiceTool, ...] = (),
        temperature: float = 0.8,
        vad_threshold: float = 0.5,
        prefix_padding_ms: int = 300,
        silence_duration_ms: int = 500,
        on_audio: Callable[[bytes], None],
        on_clear_audio: Callable[[], None],
        on_transcript: Callable[[RealtimeVoiceRole, str, bool], None] | None = None,
        on_tool_call: Callable[[RealtimeVoiceToolCallEvent], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_close: Callable[[RealtimeVoiceCloseReason], None] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model or _DEFAULT_MODEL
        self._voice = voice
        self._instructions = instructions
        self._tools = tools
        self._temperature = temperature
        self._vad_threshold = vad_threshold
        self._prefix_padding_ms = prefix_padding_ms
        self._silence_duration_ms = silence_duration_ms

        self._on_audio = on_audio
        self._on_clear_audio = on_clear_audio
        self._on_transcript = on_transcript
        self._on_tool_call = on_tool_call
        self._on_ready = on_ready
        self._on_error = on_error
        self._on_close = on_close

        self._ws: Any = None
        self._connected = False
        self._session_configured = False
        self._intentionally_closed = False
        self._reconnect_attempts = 0
        self._pending_audio: list[bytes] = []
        self._tool_buffers: dict[str, dict[str, str]] = {}
        self._session_ready_fired = False
        self._read_task: asyncio.Task | None = None

    # ─── public surface ──────────────────────────────────────────────

    async def connect(self) -> None:
        self._intentionally_closed = False
        self._reconnect_attempts = 0
        await self._do_connect()

    def send_audio(self, audio: bytes) -> None:
        if not self._connected or not self._session_configured or self._ws is None:
            if len(self._pending_audio) < _PENDING_AUDIO_CAP:
                self._pending_audio.append(audio)
            return
        self._send_event({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(audio).decode("ascii"),
        })

    def send_user_message(self, text: str) -> None:
        self._send_event({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        })
        self._send_event({"type": "response.create"})

    def submit_tool_result(self, call_id: str, result: Any) -> None:
        self._send_event({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result),
            },
        })
        self._send_event({"type": "response.create"})

    def trigger_greeting(self, instructions: str | None = None) -> None:
        if not self.is_connected():
            return
        self._send_event({
            "type": "response.create",
            "response": {"instructions": instructions or self._instructions},
        })

    def close(self) -> None:
        self._intentionally_closed = True
        self._connected = False
        self._session_configured = False
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                asyncio.get_running_loop().create_task(ws.close())
            except RuntimeError:
                pass  # no running loop — already torn down

    def is_connected(self) -> bool:
        return self._connected and self._session_configured

    # ─── connection lifecycle ─────────────────────────────────────────

    async def _connect_websocket(self, url: str, **kwargs: Any) -> Any:
        """Pulled out for testability — tests stub this to return a fake."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "OpenAI-Beta": "realtime=v1",
        }
        return await websockets.connect(url, additional_headers=headers, **kwargs)

    async def _do_connect(self) -> None:
        url = f"wss://api.openai.com/v1/realtime?model={quote(self._model)}"
        try:
            self._ws = await asyncio.wait_for(
                self._connect_websocket(url), timeout=_CONNECT_TIMEOUT_S,
            )
        except (TimeoutError, OSError) as exc:
            if self._on_error:
                self._on_error(exc if isinstance(exc, Exception) else Exception(str(exc)))
            return
        self._connected = True
        self._session_configured = False
        self._reconnect_attempts = 0
        self._send_session_update()
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    _log.warning("realtime event parse failed: %s", exc)
                    continue
                self._handle_event(event)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._connected = False
            self._session_configured = False
            if self._intentionally_closed:
                if self._on_close:
                    self._on_close("completed")
                return
            await self._attempt_reconnect()

    async def _attempt_reconnect(self) -> None:
        if self._intentionally_closed:
            return
        if self._reconnect_attempts >= _MAX_RECONNECT_ATTEMPTS:
            if self._on_close:
                self._on_close("error")
            return
        self._reconnect_attempts += 1
        delay = _BASE_RECONNECT_DELAY_S * (2 ** (self._reconnect_attempts - 1))
        await asyncio.sleep(delay)
        if self._intentionally_closed:
            return
        try:
            await self._do_connect()
        except Exception as exc:  # noqa: BLE001 — defensive
            if self._on_error:
                self._on_error(exc)
            await self._attempt_reconnect()

    # ─── outbound ────────────────────────────────────────────────────

    def _send_session_update(self) -> None:
        session: dict[str, Any] = {
            "modalities": ["text", "audio"],
            "voice": self._voice,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": self._vad_threshold,
                "prefix_padding_ms": self._prefix_padding_ms,
                "silence_duration_ms": self._silence_duration_ms,
                "create_response": True,
            },
            "temperature": self._temperature,
        }
        if self._instructions:
            session["instructions"] = self._instructions
        if self._tools:
            session["tools"] = [
                {
                    "type": t.type,
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in self._tools
            ]
            session["tool_choice"] = "auto"
        self._send_event({"type": "session.update", "session": session})

    def _send_event(self, event: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            asyncio.get_running_loop().create_task(ws.send(json.dumps(event)))
        except RuntimeError:
            # No running loop — drop. Caller can retry via reconnect.
            pass

    # ─── inbound ─────────────────────────────────────────────────────

    def _handle_event(self, event: dict[str, Any]) -> None:
        et = event.get("type")
        if et == "session.created":
            return
        if et == "session.updated":
            self._session_configured = True
            for chunk in self._pending_audio:
                self.send_audio(chunk)
            self._pending_audio.clear()
            if not self._session_ready_fired:
                self._session_ready_fired = True
                if self._on_ready:
                    self._on_ready()
            return
        if et == "response.audio.delta":
            delta = event.get("delta")
            if not delta:
                return
            try:
                audio = base64.b64decode(delta)
            except (ValueError, TypeError):
                return
            self._on_audio(audio)
            return
        if et == "input_audio_buffer.speech_started":
            self._on_clear_audio()
            return
        if et == "response.audio_transcript.delta":
            delta = event.get("delta")
            if delta and self._on_transcript:
                self._on_transcript("assistant", delta, False)
            return
        if et == "response.audio_transcript.done":
            transcript = event.get("transcript")
            if transcript and self._on_transcript:
                self._on_transcript("assistant", transcript, True)
            return
        if et == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript")
            if transcript and self._on_transcript:
                self._on_transcript("user", transcript, True)
            return
        if et == "conversation.item.input_audio_transcription.delta":
            delta = event.get("delta")
            if delta and self._on_transcript:
                self._on_transcript("user", delta, False)
            return
        if et == "response.function_call_arguments.delta":
            key = event.get("item_id") or "unknown"
            existing = self._tool_buffers.get(key)
            if existing:
                existing["args"] += event.get("delta") or ""
            elif event.get("item_id"):
                self._tool_buffers[event["item_id"]] = {
                    "name": event.get("name") or "",
                    "call_id": event.get("call_id") or "",
                    "args": event.get("delta") or "",
                }
            return
        if et == "response.function_call_arguments.done":
            key = event.get("item_id") or "unknown"
            buffered = self._tool_buffers.get(key)
            if self._on_tool_call:
                raw_args = (
                    (buffered.get("args") if buffered else None)
                    or event.get("arguments")
                    or "{}"
                )
                try:
                    args = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                self._on_tool_call(RealtimeVoiceToolCallEvent(
                    item_id=key,
                    call_id=(buffered.get("call_id") if buffered else None) or event.get("call_id") or "",
                    name=(buffered.get("name") if buffered else None) or event.get("name") or "",
                    args=args,
                ))
            self._tool_buffers.pop(key, None)
            return
        if et == "error":
            detail = read_realtime_error_detail(event.get("error"))
            if self._on_error:
                self._on_error(Exception(detail))
            return
        # Unknown event types: silently ignore (forward-compat with
        # OpenAI adding new event kinds — same as the TS default branch).


__all__ = ["OpenAIRealtimeBridge"]
