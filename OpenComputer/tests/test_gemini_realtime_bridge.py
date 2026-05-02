"""Gemini Realtime bridge — unit tests.

Mirrors test_openai_realtime_bridge.py shape: fake WebSocket, push inbound
frames, assert outbound shape + callback delivery. Focused on Gemini-specific
behavior (setup message, tool-call name caching, native-rate audio forward,
transcript finals on turnComplete).
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest


class _FakeWS:
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


def _make_bridge(callbacks: dict[str, Any] | None = None, **overrides: Any) -> Any:
    from extensions.gemini_provider.realtime import GeminiRealtimeBridge

    cb = callbacks or {}
    kwargs: dict[str, Any] = dict(
        api_key="key-test",
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
    kwargs.update(overrides)
    return GeminiRealtimeBridge(**kwargs)


# ─── outbound shape ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_message_shape_on_open() -> None:
    """When the WS opens, the bridge sends a setup with all expected keys."""
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        # Auth key must be in the URL, not headers.
        assert "key=key-test" in url
        return fake_ws

    b = _make_bridge()
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"setupComplete": {}})
    await asyncio.sleep(0.05)
    fake_ws._inbound.put_nowait(None)
    await task

    assert fake_ws.sent, "expected setup message to be sent"
    setup_event = json.loads(fake_ws.sent[0])
    assert "setup" in setup_event
    setup = setup_event["setup"]
    assert setup["model"].startswith("models/gemini-")
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert setup["realtimeInputConfig"]["activityHandling"] == "START_OF_ACTIVITY_INTERRUPTS"
    assert "automaticActivityDetection" in setup["realtimeInputConfig"]
    assert setup["systemInstruction"]["parts"][0]["text"] == "be helpful"
    assert setup["inputAudioTranscription"] == {}
    assert setup["outputAudioTranscription"] == {}


@pytest.mark.asyncio
async def test_send_audio_wraps_realtimeInput_pcm16k() -> None:  # noqa: N802 — name mirrors realtime API "realtimeInput" wire field
    """send_audio after ready should emit realtimeInput.audio with 16kHz mime."""
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    ready_evt = asyncio.Event()
    b = _make_bridge({"on_ready": lambda: ready_evt.set()})
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"setupComplete": {}})
    await asyncio.wait_for(ready_evt.wait(), timeout=1.0)

    b.send_audio(b"\x00\x01\x02\x03")
    await asyncio.sleep(0.02)

    # Filter on top-level key, not substring — the setup message contains
    # ``realtimeInputConfig`` which would false-match a naive substring check.
    parsed = [json.loads(s) for s in fake_ws.sent]
    audio_frames = [m for m in parsed if "realtimeInput" in m]
    assert audio_frames, "expected at least one realtimeInput frame"
    audio = audio_frames[0]["realtimeInput"]["audio"]
    assert audio["mimeType"] == "audio/pcm;rate=16000"
    assert base64.b64decode(audio["data"]) == b"\x00\x01\x02\x03"

    fake_ws._inbound.put_nowait(None)
    await task


@pytest.mark.asyncio
async def test_send_audio_from_non_loop_thread_dispatches_via_runcoroutinethreadsafe() -> None:
    """Mic chunks called from the sounddevice audio thread must reach the WS.

    Regression for the cross-thread asyncio bug: the previous
    single-path ``asyncio.get_running_loop().create_task(...)`` raised
    ``RuntimeError`` when called from a non-loop thread (e.g. the
    sounddevice mic callback) and silently dropped the chunk. After the
    fix, ``_send_event`` detects the cross-thread case and dispatches
    via ``asyncio.run_coroutine_threadsafe`` onto the loop captured at
    connect time.
    """
    import threading

    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    ready_evt = asyncio.Event()
    b = _make_bridge({"on_ready": lambda: ready_evt.set()})
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"setupComplete": {}})
    await asyncio.wait_for(ready_evt.wait(), timeout=1.0)

    # Drive send_audio from a different OS thread — like sounddevice's
    # audio callback would. The bridge must accept the call and schedule
    # the WS write on its captured loop.
    chunk = b"\xde\xad\xbe\xef"

    def worker() -> None:
        b.send_audio(chunk)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=1.0)
    assert not t.is_alive(), "worker thread should have returned"

    # Allow the loop to drain the threadsafe-scheduled coroutine.
    for _ in range(20):
        await asyncio.sleep(0.02)
        parsed = [json.loads(s) for s in fake_ws.sent]
        audio_frames = [m for m in parsed if "realtimeInput" in m]
        if audio_frames:
            break
    else:
        pytest.fail("audio frame never reached fake WS — cross-thread send dropped")

    audio = audio_frames[0]["realtimeInput"]["audio"]
    assert base64.b64decode(audio["data"]) == chunk

    fake_ws._inbound.put_nowait(None)
    await task


# ─── inbound dispatch ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_complete_fires_on_ready_and_flushes_pending() -> None:
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    ready_evt = asyncio.Event()
    b = _make_bridge({"on_ready": lambda: ready_evt.set()})
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    # Queue audio BEFORE ready — should be buffered, then flushed on ready.
    b.send_audio(b"\xff\xfe")

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"setupComplete": {}})
    await asyncio.wait_for(ready_evt.wait(), timeout=1.0)
    await asyncio.sleep(0.02)

    # Top-level-key filter — substring check would false-match the setup
    # message's ``realtimeInputConfig``.
    audio_frames = [m for m in (json.loads(s) for s in fake_ws.sent) if "realtimeInput" in m]
    assert len(audio_frames) == 1
    assert base64.b64decode(audio_frames[0]["realtimeInput"]["audio"]["data"]) == b"\xff\xfe"

    fake_ws._inbound.put_nowait(None)
    await task


@pytest.mark.asyncio
async def test_audio_24k_forwarded_as_native_pcm() -> None:
    """Inbound model audio is 24 kHz PCM and forwarded as-is.

    No in-bridge resample anymore — the CLI configures LocalAudioIO with
    ``output_sample_rate=24_000`` for this provider so playback is at the
    native rate. Bridge stays simple; sink absorbs the rate.
    """
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    received: list[bytes] = []
    ready_evt = asyncio.Event()
    b = _make_bridge({
        "on_audio": lambda chunk: received.append(chunk),
        "on_ready": lambda: ready_evt.set(),
    })
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"setupComplete": {}})
    await asyncio.wait_for(ready_evt.wait(), timeout=1.0)

    # 30 PCM16 samples @ 24 kHz — exact bytes preserved into on_audio.
    src_samples = bytes(range(60))  # 30 int16 samples (60 bytes)
    fake_ws.push({
        "serverContent": {
            "modelTurn": {
                "parts": [{
                    "inlineData": {
                        "mimeType": "audio/pcm;rate=24000",
                        "data": base64.b64encode(src_samples).decode("ascii"),
                    },
                }],
            },
        },
    })
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0] == src_samples  # byte-for-byte, no resample

    fake_ws._inbound.put_nowait(None)
    await task


@pytest.mark.asyncio
async def test_interrupted_fires_on_clear_audio() -> None:
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    cleared = asyncio.Event()
    ready_evt = asyncio.Event()
    b = _make_bridge({
        "on_clear_audio": lambda: cleared.set(),
        "on_ready": lambda: ready_evt.set(),
    })
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"setupComplete": {}})
    await asyncio.wait_for(ready_evt.wait(), timeout=1.0)

    fake_ws.push({"serverContent": {"interrupted": True}})
    await asyncio.wait_for(cleared.wait(), timeout=1.0)

    fake_ws._inbound.put_nowait(None)
    await task


# ─── tool-call round trip ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_call_then_submit_includes_name_on_wire() -> None:
    """Bridge caches name from inbound toolCall and echoes it in toolResponse."""
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    received_calls: list[Any] = []
    ready_evt = asyncio.Event()
    b = _make_bridge({
        "on_tool_call": lambda evt: received_calls.append(evt),
        "on_ready": lambda: ready_evt.set(),
    })
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"setupComplete": {}})
    await asyncio.wait_for(ready_evt.wait(), timeout=1.0)

    fake_ws.push({
        "toolCall": {
            "functionCalls": [
                {"id": "call-42", "name": "get_weather", "args": {"city": "NYC"}},
            ],
        },
    })
    await asyncio.sleep(0.02)

    assert len(received_calls) == 1
    evt = received_calls[0]
    assert evt.call_id == "call-42"
    assert evt.name == "get_weather"
    assert evt.args == {"city": "NYC"}

    # Submit result — Gemini wire requires the function name; bridge looks
    # it up from the cache populated above.
    b.submit_tool_result("call-42", {"temp": 72})
    await asyncio.sleep(0.02)

    tool_responses = [m for m in (json.loads(s) for s in fake_ws.sent) if "toolResponse" in m]
    assert len(tool_responses) == 1
    fr = tool_responses[0]["toolResponse"]["functionResponses"][0]
    assert fr["id"] == "call-42"
    assert fr["name"] == "get_weather"
    assert fr["response"] == {"temp": 72}

    fake_ws._inbound.put_nowait(None)
    await task


@pytest.mark.asyncio
async def test_submit_tool_result_wraps_scalar_as_result_dict() -> None:
    """Non-dict results are wrapped as {'result': value}."""
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    ready_evt = asyncio.Event()
    b = _make_bridge({"on_ready": lambda: ready_evt.set()})
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"setupComplete": {}})
    await asyncio.wait_for(ready_evt.wait(), timeout=1.0)

    fake_ws.push({
        "toolCall": {
            "functionCalls": [
                {"id": "c1", "name": "echo", "args": {}},
            ],
        },
    })
    await asyncio.sleep(0.02)

    b.submit_tool_result("c1", "plain string output")
    await asyncio.sleep(0.02)

    tool_responses = [m for m in (json.loads(s) for s in fake_ws.sent) if "toolResponse" in m]
    assert tool_responses[0]["toolResponse"]["functionResponses"][0]["response"] == {
        "result": "plain string output",
    }

    fake_ws._inbound.put_nowait(None)
    await task


# ─── transcripts ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_input_transcript_chunk_is_final_false() -> None:
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    transcripts: list[tuple[str, str, bool]] = []
    ready_evt = asyncio.Event()
    b = _make_bridge({
        "on_transcript": lambda role, text, final: transcripts.append((role, text, final)),
        "on_ready": lambda: ready_evt.set(),
    })
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.02)
    fake_ws.push({"setupComplete": {}})
    await asyncio.wait_for(ready_evt.wait(), timeout=1.0)

    fake_ws.push({
        "serverContent": {"inputTranscription": {"text": "hello "}},
    })
    fake_ws.push({
        "serverContent": {"inputTranscription": {"text": "world"}, "turnComplete": True},
    })
    await asyncio.sleep(0.05)

    user_chunks = [t for t in transcripts if t[0] == "user"]
    assert ("user", "hello ", False) in user_chunks
    assert ("user", "world", False) in user_chunks
    # turnComplete fires synthetic empty-string finals for each role.
    assert ("user", "", True) in transcripts
    assert ("assistant", "", True) in transcripts

    fake_ws._inbound.put_nowait(None)
    await task


# ─── helpers ─────────────────────────────────────────────────────────


def test_vad_threshold_to_sensitivity_buckets() -> None:
    """Gemini's enum has only HIGH/LOW per boundary — no MEDIUM. Confirmed
    by Gemini WS rejecting ``START_SENSITIVITY_MEDIUM`` with frame 1007
    'invalid frame payload data'. The mapping is binary: below 0.5 →
    eager-start + patient-end; at or above 0.5 → conservative-start +
    quick-end."""
    from extensions.gemini_provider.realtime_helpers import (
        vad_threshold_to_sensitivity,
    )

    assert vad_threshold_to_sensitivity(0.0) == ("START_SENSITIVITY_HIGH", "END_SENSITIVITY_LOW")
    assert vad_threshold_to_sensitivity(0.1) == ("START_SENSITIVITY_HIGH", "END_SENSITIVITY_LOW")
    assert vad_threshold_to_sensitivity(0.49) == ("START_SENSITIVITY_HIGH", "END_SENSITIVITY_LOW")
    # Boundary is exclusive at 0.5 — at exactly 0.5 the conservative side wins.
    assert vad_threshold_to_sensitivity(0.5) == ("START_SENSITIVITY_LOW", "END_SENSITIVITY_HIGH")
    assert vad_threshold_to_sensitivity(0.9) == ("START_SENSITIVITY_LOW", "END_SENSITIVITY_HIGH")
    assert vad_threshold_to_sensitivity(1.0) == ("START_SENSITIVITY_LOW", "END_SENSITIVITY_HIGH")


def test_realtime_tool_call_event_extra_default_is_per_instance() -> None:
    """The new ``extra`` field defaults to a fresh dict per instance.

    Frozen + slots; default_factory=dict gives independent dicts (no
    shared-mutable-default footgun).
    """
    from plugin_sdk.realtime_voice import RealtimeVoiceToolCallEvent

    a = RealtimeVoiceToolCallEvent(item_id="x", call_id="x", name="n", args={})
    b = RealtimeVoiceToolCallEvent(item_id="y", call_id="y", name="n", args={})
    assert a.extra == {} and b.extra == {}
    assert a.extra is not b.extra
    # Constructor accepts a populated extra.
    c = RealtimeVoiceToolCallEvent(
        item_id="z", call_id="z", name="n", args={}, extra={"response_id": "r1"},
    )
    assert c.extra == {"response_id": "r1"}


def test_realtime_tool_call_event_item_id_optional() -> None:
    """Providers without a separate item-id concept (Gemini, future
    Anthropic) can construct events without item_id — defaults to None."""
    from plugin_sdk.realtime_voice import RealtimeVoiceToolCallEvent

    # Construct WITHOUT item_id — minimum required is call_id, name, args.
    e = RealtimeVoiceToolCallEvent(call_id="c1", name="get_weather", args={})
    assert e.call_id == "c1"
    assert e.name == "get_weather"
    assert e.args == {}
    assert e.item_id is None
    assert e.extra == {}

    # Verify Gemini bridge actually emits item_id=None on real tool calls.
    from extensions.gemini_provider.realtime import GeminiRealtimeBridge

    received: list[Any] = []
    g = GeminiRealtimeBridge(
        api_key="k", instructions=None,
        on_audio=lambda x: None, on_clear_audio=lambda: None,
        on_tool_call=lambda evt: received.append(evt),
    )
    g._connected = True
    g._session_configured = True
    g._session_ready_fired = True
    g._handle_event({"toolCall": {"functionCalls": [
        {"id": "cid-9", "name": "do_thing", "args": {}},
    ]}})
    assert len(received) == 1
    assert received[0].call_id == "cid-9"
    assert received[0].item_id is None  # Gemini doesn't have item-id concept
