"""Gemini Live realtime WebSocket bridge.

Concrete ``BaseRealtimeVoiceBridge`` for Google's Gemini Live
BidiGenerateContent endpoint.

Behavioral diffs vs ``OpenAIRealtimeBridge`` (extensions/openai-provider/
realtime.py) — read these before touching:

* Auth: API key in URL query (``?key=<KEY>``). No Authorization header.
* Audio out is **24 kHz** (OpenAI is 16 kHz both ways). Forwarded as-is;
  the CLI configures ``LocalAudioIO(output_sample_rate=GeminiRealtimeBridge.OUTPUT_RATE_HZ)``
  so playback runs at native rate. Module exports ``OUTPUT_RATE_HZ`` for
  callers that need to size their audio sink.
* Setup signal: top-level ``setupComplete`` instead of ``session.updated``.
* No ``response.create``. The model auto-responds after each user turn.
  ``trigger_greeting`` injects a synthetic ``clientContent`` user turn.
* Tool calls arrive at top level as ``toolCall.functionCalls[]`` with
  args already parsed (dicts) — no buffering loop / no JSON-decode.
* Tool result requires the function NAME on the wire — OC's ABC only
  exposes ``call_id``. We cache name keyed by call_id when the tool call
  arrives and look it up on ``submit_tool_result``.
* VAD config is a sensitivity enum, not a float threshold. Mapped via
  ``vad_threshold_to_sensitivity``.
* Barge-in: ``serverContent.interrupted: true`` (OpenAI sends
  ``input_audio_buffer.speech_started``).
* Transcripts have no per-role "done" event; we forward each chunk as
  ``final=False`` and emit empty-string finals on ``turnComplete``.
* GoAway: server-closing notice; logged + ConnectionClosed handles the rest.
* Reconnect: 5 attempts, exponential backoff (matches OpenAI bridge).
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
from websockets.exceptions import ConnectionClosed

try:
    from realtime_helpers import (  # plugin-loader mode (sibling import)
        read_realtime_error_detail,
        vad_threshold_to_sensitivity,
    )
except ImportError:  # pragma: no cover — package mode (tests use this path)
    from extensions.gemini_provider.realtime_helpers import (
        read_realtime_error_detail,
        vad_threshold_to_sensitivity,
    )

from plugin_sdk.realtime_voice import (
    BaseRealtimeVoiceBridge,
    RealtimeVoiceCloseReason,
    RealtimeVoiceRole,
    RealtimeVoiceTool,
    RealtimeVoiceToolCallEvent,
)

_log = logging.getLogger("opencomputer.providers.gemini.realtime")

_WS_BASE = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
_DEFAULT_MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"

_INPUT_RATE_HZ = 16_000   # mic → Gemini
OUTPUT_RATE_HZ = 24_000   # Gemini → speaker (consumed directly by LocalAudioIO when configured for 24k)

_MAX_RECONNECT_ATTEMPTS = 5
_BASE_RECONNECT_DELAY_S = 1.0
_CONNECT_TIMEOUT_S = 10.0
_PENDING_AUDIO_CAP = 320


class GeminiRealtimeBridge(BaseRealtimeVoiceBridge):
    """Concrete realtime bridge for Gemini Live BidiGenerateContent."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None = None,
        instructions: str | None = None,
        tools: tuple[RealtimeVoiceTool, ...] = (),
        vad_threshold: float = 0.5,
        prefix_padding_ms: int = 40,
        silence_duration_ms: int = 500,
        thinking_budget: int = 0,
        enable_transcription: bool = True,
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
        self._instructions = instructions
        self._tools = tools
        self._vad_threshold = vad_threshold
        self._prefix_padding_ms = prefix_padding_ms
        self._silence_duration_ms = silence_duration_ms
        self._thinking_budget = thinking_budget
        self._enable_transcription = enable_transcription

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
        # Gemini wire requires the function NAME on submit_tool_result; OC's
        # ABC only gives us call_id. Cache when the tool call arrives.
        self._call_id_to_name: dict[str, str] = {}
        self._session_ready_fired = False
        self._read_task: asyncio.Task | None = None
        # Captured at connect time so cross-thread callers (e.g. sounddevice
        # mic callbacks) can dispatch back onto the asyncio loop via
        # ``asyncio.run_coroutine_threadsafe``. Without this the audio
        # thread's call to ``_send_event`` would silently drop chunks.
        self._loop: asyncio.AbstractEventLoop | None = None

    # ─── public surface (BaseRealtimeVoiceBridge) ─────────────────────

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
            "realtimeInput": {
                "audio": {
                    "mimeType": f"audio/pcm;rate={_INPUT_RATE_HZ}",
                    "data": base64.b64encode(audio).decode("ascii"),
                },
            },
        })

    def send_user_message(self, text: str) -> None:
        # Gemini uses clientContent.turns. No follow-up "response.create" —
        # the server auto-responds when turnComplete=True lands.
        self._send_event({
            "clientContent": {
                "turns": [
                    {"role": "user", "parts": [{"text": text}]},
                ],
                "turnComplete": True,
            },
        })

    def submit_tool_result(self, call_id: str, result: Any) -> None:
        # Gemini requires the function name on the wire. Look it up from
        # the cache populated when the toolCall arrived.
        name = self._call_id_to_name.pop(call_id, "")
        if not name:
            _log.warning("submit_tool_result for unknown call_id=%s", call_id)

        # ``response`` must be a dict on the wire. Wrap scalars/strings.
        response_payload = result if isinstance(result, dict) else {"result": result}

        self._send_event({
            "toolResponse": {
                "functionResponses": [
                    {
                        "id": call_id,
                        "name": name,
                        "response": response_payload,
                    },
                ],
            },
        })

    def trigger_greeting(self, instructions: str | None = None) -> None:
        if not self.is_connected():
            return
        # Gemini has no native greeting trigger. Inject a synthetic user
        # turn so the model takes the floor. The system prompt usually
        # carries the actual style; this just prods the model to start.
        prompt = instructions or "Greet the user briefly and ask how you can help."
        self._send_event({
            "clientContent": {
                "turns": [
                    {"role": "user", "parts": [{"text": prompt}]},
                ],
                "turnComplete": True,
            },
        })

    def close(self) -> None:
        self._intentionally_closed = True
        self._connected = False
        self._session_configured = False
        ws = self._ws
        self._ws = None
        loop = self._loop
        if ws is None or loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(ws.close())
        else:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except RuntimeError:
                pass

    def is_connected(self) -> bool:
        return self._connected and self._session_configured

    # ─── connection lifecycle ─────────────────────────────────────────

    async def _connect_websocket(self, url: str, **kwargs: Any) -> Any:
        """Pulled out for testability — tests stub this to return a fake."""
        # Gemini auths via query param; no headers needed.
        return await websockets.connect(url, **kwargs)

    async def _do_connect(self) -> None:
        url = f"{_WS_BASE}?key={quote(self._api_key)}"
        try:
            self._ws = await asyncio.wait_for(
                self._connect_websocket(url), timeout=_CONNECT_TIMEOUT_S,
            )
        except (TimeoutError, OSError) as exc:
            if self._on_error:
                self._on_error(exc if isinstance(exc, Exception) else Exception(str(exc)))
            return
        # Capture the running loop so cross-thread send paths (mic
        # callback on the sounddevice audio thread) can dispatch back
        # onto it via ``run_coroutine_threadsafe``. Stored AFTER the
        # successful connect so a connect failure doesn't leave a stale
        # reference for ``close()`` to chase.
        self._loop = asyncio.get_running_loop()
        self._connected = True
        self._session_configured = False
        self._reconnect_attempts = 0
        self._send_setup_message()
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                # Gemini sends both string and binary frames. websockets-py
                # yields str OR bytes; normalise to str for json.loads.
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    _log.warning("realtime event parse failed: %s", exc)
                    continue
                self._handle_event(event)
        except ConnectionClosed:
            pass
        # Same B012 dance as openai-provider/realtime.py — no try/finally.
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

    def _send_setup_message(self) -> None:
        start_sens, end_sens = vad_threshold_to_sensitivity(self._vad_threshold)

        setup: dict[str, Any] = {
            "model": self._model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "thinkingConfig": {"thinkingBudget": self._thinking_budget},
            },
            "realtimeInputConfig": {
                "automaticActivityDetection": {
                    "disabled": False,
                    "startOfSpeechSensitivity": start_sens,
                    "endOfSpeechSensitivity": end_sens,
                    "silenceDurationMs": self._silence_duration_ms,
                    "prefixPaddingMs": self._prefix_padding_ms,
                },
                "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
                "turnCoverage": "TURN_INCLUDES_ALL_INPUT",
            },
        }
        if self._enable_transcription:
            # Empty objects = enable transcription with default settings.
            # Disabling shaves ~50-100ms server-side (model doesn't have
            # to run STT/TTS-text in parallel) — useful when the caller
            # only consumes audio, never the text transcript.
            setup["inputAudioTranscription"] = {}
            setup["outputAudioTranscription"] = {}
        if self._instructions:
            setup["systemInstruction"] = {"parts": [{"text": self._instructions}]}
        if self._tools:
            # Gemini's tools shape: [{functionDeclarations: [...]}]. Drop
            # the per-tool ``type`` field — Gemini infers it.
            setup["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        }
                        for t in self._tools
                    ],
                },
            ]

        self._send_event({"setup": setup})

    def _send_event(self, event: dict[str, Any]) -> None:
        """Schedule a JSON frame on the WebSocket from any thread.

        The mic callback runs on sounddevice's audio thread, so this MUST
        work from a non-loop thread. We dispatch via the loop captured
        in ``_do_connect``: same-thread → ``loop.create_task``,
        cross-thread → ``asyncio.run_coroutine_threadsafe``. The previous
        single-path ``asyncio.get_running_loop().create_task(...)``
        silently dropped frames whenever it was called off-loop.
        """
        ws = self._ws
        loop = self._loop
        if ws is None or loop is None or loop.is_closed():
            return
        payload = json.dumps(event)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(ws.send(payload))
        else:
            # Different OS thread (e.g. sounddevice mic callback).
            # ``run_coroutine_threadsafe`` returns a concurrent future
            # that we deliberately don't await — fire-and-forget,
            # consistent with the old behavior. Errors during send
            # bubble up via the read loop's ConnectionClosed handler.
            try:
                asyncio.run_coroutine_threadsafe(ws.send(payload), loop)
            except RuntimeError:
                # Loop closed between the is_closed() check and the
                # submission. Drop silently — the next reconnect will
                # rebuild the loop reference.
                pass

    # ─── inbound dispatch ────────────────────────────────────────────

    def _handle_event(self, event: dict[str, Any]) -> None:
        # 1) Setup complete — session is ready to accept audio.
        if "setupComplete" in event:
            self._session_configured = True
            for chunk in self._pending_audio:
                self.send_audio(chunk)
            self._pending_audio.clear()
            if not self._session_ready_fired:
                self._session_ready_fired = True
                if self._on_ready:
                    self._on_ready()
            return

        # 2) GoAway — server intends to close. Log; ConnectionClosed will
        #    fire shortly via the read loop, which kicks reconnect.
        if "goAway" in event:
            time_left = event["goAway"].get("timeLeft", {})
            secs = time_left.get("seconds", "?")
            _log.info("Gemini goAway received, server closing in ~%ss", secs)
            return

        # 3) Tool call (top-level). Args arrive already-parsed.
        if "toolCall" in event:
            calls = event["toolCall"].get("functionCalls") or []
            for call in calls:
                call_id = call.get("id") or ""
                name = call.get("name") or ""
                args = call.get("args") or {}
                if not call_id or not name:
                    continue
                self._call_id_to_name[call_id] = name
                if self._on_tool_call:
                    # Gemini has no separate item-id concept; let item_id
                    # default to None per the SDK contract.
                    self._on_tool_call(RealtimeVoiceToolCallEvent(
                        call_id=call_id,
                        name=name,
                        args=args,
                    ))
            return

        # 4) Tool call cancellation — user interrupted mid tool execution.
        #    OC's ABC has no cancellation callback; just clean up the
        #    name cache. The session orchestrator may still post a stale
        #    submit_tool_result, which the lookup will warn about.
        if "toolCallCancellation" in event:
            ids = event["toolCallCancellation"].get("ids") or []
            for cid in ids:
                self._call_id_to_name.pop(cid, None)
            _log.info("Gemini toolCallCancellation: %s", ",".join(ids))
            return

        # 5) Error — surface via on_error.
        if "error" in event and isinstance(event["error"], dict):
            detail = read_realtime_error_detail(event.get("error"))
            if self._on_error:
                self._on_error(Exception(detail))
            return

        # 6) Server content — audio chunks, transcripts, turn boundaries.
        server_content = event.get("serverContent")
        if server_content is None:
            return  # unknown event kind — forward-compat, ignore

        # 6a) Barge-in: user started talking while model was speaking.
        if server_content.get("interrupted") is True:
            self._on_clear_audio()
            return

        # 6b) Model turn parts — audio (PCM 24 kHz) and text.
        model_turn = server_content.get("modelTurn")
        if model_turn:
            for part in model_turn.get("parts") or []:
                inline = part.get("inlineData")
                if inline:
                    mime = inline.get("mimeType") or ""
                    if mime.startswith("audio/pcm") and (b64 := inline.get("data")):
                        try:
                            audio = base64.b64decode(b64)
                        except (ValueError, TypeError):
                            continue
                        # PCM16 24 kHz, forwarded as-is. The CLI configures
                        # LocalAudioIO with output_sample_rate=24_000 for
                        # this provider so playback is at native rate.
                        self._on_audio(audio)
                elif (text := part.get("text")):
                    if self._on_transcript:
                        self._on_transcript("assistant", text, False)

        # 6c) Transcripts (live captions). No "done" event — chunks arrive
        #     mid-turn; turnComplete latches finality below.
        if (it := server_content.get("inputTranscription")) and (text := it.get("text")):
            if self._on_transcript:
                self._on_transcript("user", text, False)

        if (ot := server_content.get("outputTranscription")) and (text := ot.get("text")):
            if self._on_transcript:
                self._on_transcript("assistant", text, False)

        # 6d) Turn complete — model finished speaking. Emit empty-string
        #     finals so consumers can latch a UI flush per role.
        if server_content.get("turnComplete") is True:
            if self._on_transcript:
                self._on_transcript("user", "", True)
                self._on_transcript("assistant", "", True)


__all__ = ["GeminiRealtimeBridge"]
