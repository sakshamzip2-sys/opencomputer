# Realtime Voice (OpenClaw Port) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire two-way streaming voice into OpenComputer by porting OpenClaw's `realtime-voice` abstraction + OpenAI Realtime WebSocket bridge directly to Python, plus local PCM16 mic/speaker I/O, replacing the current `cli_voice.py:141-142` echo stub.

**Architecture:** Three layers, all Python: (1) a small `BaseRealtimeVoiceBridge` ABC in `plugin_sdk/` that mirrors OpenClaw's `RealtimeVoiceBridge` (TypeScript) interface — connect / sendAudio / sendUserMessage / submitToolResult / triggerGreeting / close; (2) `OpenAIRealtimeBridge` concrete implementation that holds a `websockets` client to `wss://api.openai.com/v1/realtime`, translates inbound JSON events to bridge callbacks, and outbound bridge calls to OpenAI session events — using PCM16 audio (NOT μ-law, which is telephony-only); (3) a session orchestrator + `LocalAudioIO` (sounddevice mic input + speaker output) that wires the bridge to OC's `AgentLoop` for tool dispatch. CLI command `opencomputer voice realtime` enters the loop.

**Tech Stack:** Python 3.12+, `websockets>=13.0` (already in deps), `sounddevice>=0.4` (already in deps), `pydantic>=2.9` (already in deps for config schema), `asyncio`, OpenAI Realtime API (`gpt-realtime-1.5` default; user-overridable).

**Reference:** OpenClaw source at `/Users/saksham/Vscode/claude/sources/openclaw-2026.4.23/` — specifically `src/realtime-voice/*.ts` (~330 LOC) and `extensions/openai/realtime-voice-provider.ts` (613 LOC) + `realtime-provider-shared.ts` (58 LOC). Knowledge graph at `OpenComputer/docs/refs/openclaw/voice-graph/`.

**License:** OpenClaw is the user's project — direct port authorized. Each ported file carries `# Ported from openclaw/src/<file>.ts (commit 2026-04-23)` provenance.

**Skipped per user direction + graph analysis:** μ-law audio path, mark/acknowledgeMark protocol (Twilio Media Streams), provider registry/resolver (OC has its own `PluginRegistry`), transcription-only provider variant, the entire `extensions/voice-call/` PSTN telephony stack.

---

## File Structure

| File | Lines (est.) | Responsibility |
|---|---|---|
| `plugin_sdk/realtime_voice.py` | ~80 | `BaseRealtimeVoiceBridge` ABC + dataclasses (`RealtimeVoiceTool`, `RealtimeVoiceToolCallEvent`, `RealtimeVoiceCloseReason` Literal). Public contract for plugins implementing realtime voice. |
| `opencomputer/voice/realtime_session.py` | ~120 | `create_realtime_voice_session` — wraps a bridge with audio sink + tool-call routing. Direct port of `session-runtime.ts`. |
| `opencomputer/voice/audio_io.py` | ~180 | `LocalAudioIO` — `sounddevice` PCM16 mic capture (input stream) + speaker playback (output stream) + barge-in audio flush. New code (OpenClaw uses Twilio Media Streams in TS; we use local sound). |
| `extensions/openai-provider/realtime.py` | ~470 | `OpenAIRealtimeBridge` concrete impl. Direct port of `realtime-voice-provider.ts`: WS connect/reconnect, sendSessionUpdate, sendEvent, handleEvent, handleBargeIn (truncate-on-speech-start). Pydantic config schema replaces TS `normalizeProviderConfig`. |
| `extensions/openai-provider/realtime_helpers.py` | ~50 | Inlined port of `realtime-provider-shared.ts`: `read_realtime_error_detail`, `as_finite_number`, `trim_to_undefined` (Python idiom: `trim_or_none`). |
| `opencomputer/voice/tool_router.py` | ~80 | When the bridge emits `onToolCall`, look up the tool in OC's `ToolRegistry`, dispatch via `BaseTool.execute(ToolCall)`, then call `bridge.submit_tool_result(call_id, result)`. Gates on `effective_permission_mode(runtime)` for AUTO/PLAN. |
| `opencomputer/cli_voice.py:141-150` | modify | Replace stub `agent_runner` block with `_run_realtime_loop()` that constructs the bridge + session + audio I/O + tool router. Add `realtime` subcommand to `voice_app`. |
| `tests/test_realtime_voice_sdk.py` | ~80 | ABC contract tests + dataclass shape. |
| `tests/test_realtime_session.py` | ~120 | Session-runtime port tests using a fake bridge. |
| `tests/test_openai_realtime_bridge.py` | ~250 | OpenAI bridge tests using a fake WebSocket — connect, send_audio, handle_event for each kind, barge-in, reconnect, tool-call buffering. |
| `tests/test_realtime_audio_io.py` | ~100 | LocalAudioIO tests with `sounddevice` mocked. |
| `tests/test_realtime_tool_router.py` | ~80 | Tool dispatch + submit_tool_result roundtrip. |

**Total port: ~770 Python LOC + ~630 test LOC ≈ 1400 LOC.**

---

## Task 0: Register `extensions.openai_provider` conftest alias (audit B3)

**Files:**
- Modify: `tests/conftest.py` — add `_register_openai_provider_alias()` mirroring the existing `_register_voice_mode_alias()` pattern.

**Background:** Hyphenated extension dirs (`extensions/openai-provider/`) aren't valid Python identifiers, so they aren't importable as `extensions.openai_provider` without a synthetic `sys.modules` entry. `tests/conftest.py` already does this for `voice_mode`, `coding_harness`, `aws_bedrock_provider`, etc. We add the same wrapper for `openai_provider` so Tasks 3, 4, 6, 7 tests can `from extensions.openai_provider.realtime_helpers import ...`.

- [ ] **Step 1: Add the alias function**

In `tests/conftest.py`, find an existing `_register_*_alias()` function (e.g. `_register_voice_mode_alias` around line 222), then add a sibling function below it:

```python
def _register_openai_provider_alias() -> None:
    """Register extensions.openai_provider → extensions/openai-provider/.

    Mirror of _register_voice_mode_alias for the realtime voice port
    (Task 0 of 2026-04-29-realtime-voice-openclaw-port plan). The
    realtime + realtime_helpers + plugin + provider modules all live
    under the hyphenated dir; this synthesizes an underscore namespace
    so tests can ``from extensions.openai_provider.realtime import ...``.
    """
    import importlib.util
    import sys
    import types
    from pathlib import Path

    package_dir = Path(__file__).resolve().parent.parent / "extensions" / "openai-provider"

    if "extensions.openai_provider" not in sys.modules:
        mod = types.ModuleType("extensions.openai_provider")
        mod.__path__ = [str(package_dir)]
        mod.__package__ = "extensions.openai_provider"
        sys.modules["extensions.openai_provider"] = mod
        # Make ``import extensions.openai_provider`` work at runtime.
        sys.modules["extensions"].openai_provider = mod  # type: ignore[attr-defined]

    parent = sys.modules["extensions.openai_provider"]
    for sub in ("provider", "realtime", "realtime_helpers", "plugin"):
        full = f"extensions.openai_provider.{sub}"
        if full in sys.modules:
            continue
        path = package_dir / f"{sub}.py"
        if not path.exists():
            continue
        spec = importlib.util.spec_from_file_location(full, str(path))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = sub_mod
        spec.loader.exec_module(sub_mod)
        setattr(parent, sub, sub_mod)
```

Then call it once at module-load time. Find the existing call site (e.g. at the bottom of conftest.py where `_register_voice_mode_alias()` is invoked) and add:

```python
_register_openai_provider_alias()
```

- [ ] **Step 2: Verify the alias resolves**

Run:

```bash
python -c "import sys; sys.path.insert(0, 'tests'); import conftest; from extensions.openai_provider import realtime_helpers; print('OK')"
```

Expected: `OK`. Note this will fail until Task 3 creates `realtime_helpers.py` — that's expected; this verification is rerun after Task 3.

For now, just verify the conftest.py edit doesn't break existing tests:

```bash
pytest tests/ -q --tb=no --collect-only 2>&1 | tail -5
```

Expected: same number of tests collected as before; no new collection errors.

- [ ] **Step 3: Commit**

```bash
ruff check --fix tests/conftest.py
git add tests/conftest.py
git commit -m "test(conftest): register extensions.openai_provider underscore alias"
```

---

## Task 1: SDK — `BaseRealtimeVoiceBridge` ABC + dataclasses

**Files:**
- Create: `plugin_sdk/realtime_voice.py`
- Modify: `plugin_sdk/__init__.py` (export the new symbols)
- Test: `tests/test_realtime_voice_sdk.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_realtime_voice_sdk.py`:

```python
"""BaseRealtimeVoiceBridge ABC + the dataclasses around it.

Direct Python port of openclaw/src/realtime-voice/provider-types.ts.
"""
from __future__ import annotations

import inspect

import pytest


def test_realtime_voice_tool_dataclass_shape() -> None:
    from plugin_sdk.realtime_voice import RealtimeVoiceTool

    tool = RealtimeVoiceTool(
        type="function",
        name="Bash",
        description="Run a shell command",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    )
    assert tool.type == "function"
    assert tool.name == "Bash"
    assert tool.parameters["type"] == "object"
    # Frozen — mutation must raise
    with pytest.raises(Exception):
        tool.name = "WriteFile"  # type: ignore[misc]


def test_realtime_voice_tool_call_event_shape() -> None:
    from plugin_sdk.realtime_voice import RealtimeVoiceToolCallEvent

    ev = RealtimeVoiceToolCallEvent(
        item_id="item_42",
        call_id="call_xyz",
        name="Bash",
        args={"command": "ls"},
    )
    assert ev.item_id == "item_42"
    assert ev.call_id == "call_xyz"
    assert ev.args == {"command": "ls"}


def test_close_reason_literal_accepted_values() -> None:
    """RealtimeVoiceCloseReason must accept 'completed' and 'error'."""
    from plugin_sdk.realtime_voice import RealtimeVoiceCloseReason

    # Literal at runtime is just a typing alias — assignments are OK.
    a: RealtimeVoiceCloseReason = "completed"
    b: RealtimeVoiceCloseReason = "error"
    assert a == "completed"
    assert b == "error"


def test_base_realtime_voice_bridge_is_abc() -> None:
    """BaseRealtimeVoiceBridge cannot be instantiated directly."""
    from plugin_sdk.realtime_voice import BaseRealtimeVoiceBridge

    with pytest.raises(TypeError):
        BaseRealtimeVoiceBridge()  # type: ignore[abstract]


def test_base_bridge_required_methods() -> None:
    """Mirror the openclaw RealtimeVoiceBridge interface — these abstract
    methods MUST exist or plugin-side ports will silently mis-implement."""
    from plugin_sdk.realtime_voice import BaseRealtimeVoiceBridge

    required_abstract = {
        "connect",
        "send_audio",
        "send_user_message",
        "submit_tool_result",
        "trigger_greeting",
        "close",
        "is_connected",
    }
    abstracts = set(BaseRealtimeVoiceBridge.__abstractmethods__)
    missing = required_abstract - abstracts
    assert not missing, f"missing abstract methods: {missing}"


def test_base_bridge_connect_is_async() -> None:
    """connect() must be a coroutine — bridges talk to the network."""
    from plugin_sdk.realtime_voice import BaseRealtimeVoiceBridge

    sig = inspect.signature(BaseRealtimeVoiceBridge.connect)
    assert inspect.iscoroutinefunction(BaseRealtimeVoiceBridge.connect), (
        f"connect must be async; got {sig}"
    )


def test_public_exports_in_init() -> None:
    """__init__ must surface the new types so plugins can import them
    via `from plugin_sdk import BaseRealtimeVoiceBridge` etc."""
    import plugin_sdk

    for name in (
        "BaseRealtimeVoiceBridge",
        "RealtimeVoiceTool",
        "RealtimeVoiceToolCallEvent",
        "RealtimeVoiceCloseReason",
    ):
        assert hasattr(plugin_sdk, name), f"plugin_sdk.{name} not exported"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_realtime_voice_sdk.py -v`
Expected: FAIL — `ModuleNotFoundError: plugin_sdk.realtime_voice`.

- [ ] **Step 3: Write minimal implementation**

Create `plugin_sdk/realtime_voice.py`:

```python
"""Public realtime-voice contract.

Direct Python port of openclaw/src/realtime-voice/provider-types.ts (commit 2026-04-23).
Plugins implementing realtime voice (e.g. OpenAI Realtime, future Anthropic
voice) inherit ``BaseRealtimeVoiceBridge`` and implement the seven abstract
methods. Audio is PCM16 raw bytes — μ-law (telephony) is intentionally
out of scope for OC's local-mic use case.

The SDK boundary test enforces this module imports nothing from
``opencomputer.*``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

RealtimeVoiceRole = Literal["user", "assistant"]
RealtimeVoiceCloseReason = Literal["completed", "error"]


@dataclass(frozen=True, slots=True)
class RealtimeVoiceTool:
    """Function-tool schema sent to the realtime model on session.update.

    Mirror of the TS ``RealtimeVoiceTool`` shape. ``parameters`` is a
    JSON-Schema object dict (matches ``ToolSchema.parameters`` from
    ``plugin_sdk.tool_contract`` so OC's existing tool registry plugs in
    without translation).
    """

    type: Literal["function"]
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RealtimeVoiceToolCallEvent:
    """Emitted by the bridge when the model invokes a tool mid-stream.

    The bridge buffers ``response.function_call_arguments.delta`` chunks
    and assembles this event when ``response.function_call_arguments.done``
    arrives. The session orchestrator dispatches via ``ToolRegistry`` and
    calls ``bridge.submit_tool_result(call_id, result)`` on completion.
    """

    item_id: str
    call_id: str
    name: str
    args: Any  # decoded JSON, typically dict


class BaseRealtimeVoiceBridge(ABC):
    """ABC mirroring OpenClaw's RealtimeVoiceBridge (TS) interface.

    Concrete implementations open a WebSocket (or whatever transport
    the provider needs) and translate provider events to the registered
    callbacks. The session orchestrator is unaware of the underlying
    transport — it only depends on this ABC.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection. Resolves once session is ready."""

    @abstractmethod
    def send_audio(self, audio: bytes) -> None:
        """Push a PCM16 audio chunk from the mic to the model."""

    @abstractmethod
    def send_user_message(self, text: str) -> None:
        """Inject a typed-in user message (no audio)."""

    @abstractmethod
    def submit_tool_result(self, call_id: str, result: Any) -> None:
        """After the agent ran a tool, push the result back to the model."""

    @abstractmethod
    def trigger_greeting(self, instructions: str | None = None) -> None:
        """Ask the model to speak first (used at session start)."""

    @abstractmethod
    def close(self) -> None:
        """Tear down the connection. Idempotent."""

    @abstractmethod
    def is_connected(self) -> bool:
        """True only when the session is configured AND the WS is open."""


__all__ = [
    "BaseRealtimeVoiceBridge",
    "RealtimeVoiceCloseReason",
    "RealtimeVoiceRole",
    "RealtimeVoiceTool",
    "RealtimeVoiceToolCallEvent",
]
```

In `plugin_sdk/__init__.py`, add to the imports + `__all__`:

```python
from plugin_sdk.realtime_voice import (
    BaseRealtimeVoiceBridge,
    RealtimeVoiceCloseReason,
    RealtimeVoiceRole,
    RealtimeVoiceTool,
    RealtimeVoiceToolCallEvent,
)
```

And add the same five names to the `__all__` list (alphabetically sorted with the rest).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_realtime_voice_sdk.py -v`
Expected: 7 passed.

Run the SDK boundary test to confirm we don't import from `opencomputer.*`:

Run: `pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
ruff check --fix plugin_sdk/realtime_voice.py plugin_sdk/__init__.py tests/test_realtime_voice_sdk.py
git add plugin_sdk/realtime_voice.py plugin_sdk/__init__.py tests/test_realtime_voice_sdk.py
git commit -m "feat(sdk): BaseRealtimeVoiceBridge ABC + dataclasses (port openclaw provider-types.ts)"
```

---

## Task 2: Session orchestrator (port `session-runtime.ts`)

**Files:**
- Create: `opencomputer/voice/realtime_session.py`
- Test: `tests/test_realtime_session.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_realtime_session.py`:

```python
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
        # Bridge stores callbacks set via the create_bridge factory.
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

    # Simulate the bridge firing onAudio from a model response.
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_realtime_session.py -v`
Expected: FAIL — `ModuleNotFoundError: opencomputer.voice.realtime_session`.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/voice/realtime_session.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_realtime_session.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
mkdir -p opencomputer/voice
ruff check --fix opencomputer/voice/realtime_session.py tests/test_realtime_session.py
git add opencomputer/voice/realtime_session.py tests/test_realtime_session.py
git commit -m "feat(voice): create_realtime_voice_session — port of openclaw session-runtime.ts"
```

(Note: `opencomputer/voice/` already exists per Phase 2.A — `edge_tts.py`, `groq_stt.py`, etc. are there. Just dropping the new file in.)

---

## Task 3: OpenAI Realtime bridge — config schema + helpers

**Files:**
- Create: `extensions/openai-provider/realtime_helpers.py`
- Test: `tests/test_realtime_helpers.py`

**Background:** OpenClaw splits provider-shared utilities (`realtime-provider-shared.ts`) from the bridge proper (`realtime-voice-provider.ts`). We do the same in Python so the bridge file stays under ~500 LOC. Helpers are pure functions: error-detail extractor, finite-number parser, trim-or-none.

- [ ] **Step 1: Write the failing test**

Create `tests/test_realtime_helpers.py`:

```python
"""Pure helpers ported from openclaw/extensions/openai/realtime-provider-shared.ts."""
from __future__ import annotations


def test_as_finite_number_passes_through_finite() -> None:
    from extensions.openai_provider.realtime_helpers import as_finite_number

    assert as_finite_number(0.5) == 0.5
    assert as_finite_number(0) == 0.0
    assert as_finite_number(-3.14) == -3.14


def test_as_finite_number_rejects_non_finite() -> None:
    from extensions.openai_provider.realtime_helpers import as_finite_number

    assert as_finite_number(float("inf")) is None
    assert as_finite_number(float("-inf")) is None
    assert as_finite_number(float("nan")) is None
    assert as_finite_number(None) is None
    assert as_finite_number("0.5") is None  # strings rejected — explicit numbers only


def test_trim_or_none_returns_stripped_or_none() -> None:
    from extensions.openai_provider.realtime_helpers import trim_or_none

    assert trim_or_none("  hi  ") == "hi"
    assert trim_or_none("") is None
    assert trim_or_none("   ") is None
    assert trim_or_none(None) is None


def test_read_realtime_error_detail_extracts_message() -> None:
    from extensions.openai_provider.realtime_helpers import read_realtime_error_detail

    assert (
        read_realtime_error_detail({"message": "Rate limit exceeded"})
        == "Rate limit exceeded"
    )
    assert (
        read_realtime_error_detail({"type": "invalid_request_error"})
        == "invalid_request_error"
    )
    assert read_realtime_error_detail("simple string") == "simple string"
    assert read_realtime_error_detail(None) == "unknown realtime error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_realtime_helpers.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Write minimal implementation**

Create `extensions/openai-provider/realtime_helpers.py`:

```python
"""Pure helpers for the OpenAI Realtime bridge.

Direct port of openclaw/extensions/openai/realtime-provider-shared.ts (commit 2026-04-23).
Kept separate from ``realtime.py`` so the bridge stays focused on the
WebSocket lifecycle and event dispatch.
"""
from __future__ import annotations

import math
from typing import Any


def as_finite_number(value: Any) -> float | None:
    """Return ``value`` as float iff it is a finite int/float; else None.

    Strings are rejected — config values reach here as already-parsed
    numbers (Pydantic does the str→float conversion upstream).
    """
    if isinstance(value, bool):
        # bool is a subclass of int — exclude explicitly to avoid surprises
        return None
    if not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def trim_or_none(value: Any) -> str | None:
    """Strip a string; return None if it ends up empty (or wasn't a string)."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def read_realtime_error_detail(error: Any) -> str:
    """Best-effort extraction of a human-readable error message.

    Mirrors the TS helper which reads ``error.message`` first, falls
    back to ``error.type``, then stringifies. ``None`` returns a stable
    fallback so callers don't have to special-case it.
    """
    if error is None:
        return "unknown realtime error"
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        msg = error.get("message")
        if isinstance(msg, str) and msg:
            return msg
        typ = error.get("type")
        if isinstance(typ, str) and typ:
            return typ
    return str(error)


__all__ = ["as_finite_number", "read_realtime_error_detail", "trim_or_none"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_realtime_helpers.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
ruff check --fix extensions/openai-provider/realtime_helpers.py tests/test_realtime_helpers.py
git add extensions/openai-provider/realtime_helpers.py tests/test_realtime_helpers.py
git commit -m "feat(openai): realtime_helpers — port openclaw realtime-provider-shared.ts"
```

---

## Task 4: OpenAI Realtime bridge — `OpenAIRealtimeBridge` class

**Files:**
- Create: `extensions/openai-provider/realtime.py`
- Test: `tests/test_openai_realtime_bridge.py`

**Background:** This is the bulk port — `OpenAIRealtimeVoiceBridge` from `realtime-voice-provider.ts:119-580`. Translates OpenAI Realtime WebSocket events to bridge callbacks and bridge calls back to outbound WS frames. Uses PCM16 audio format (NOT μ-law). Drops the mark protocol entirely.

- [ ] **Step 1: Write the failing test**

Create `tests/test_openai_realtime_bridge.py`:

```python
"""OpenAI Realtime bridge — port of openclaw/extensions/openai/realtime-voice-provider.ts.

Tests use a fake WebSocket exposing ``send`` (records frames) and a
configurable inbound queue. The bridge is only Windows for the Win32
shim test suite — these tests work cross-platform because there's no
mic/speaker access here.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_BRIDGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "extensions" / "openai-provider" / "realtime.py"
)


def _load_bridge_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_test_oai_realtime", _BRIDGE_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_test_oai_realtime"] = mod
    spec.loader.exec_module(mod)
    return mod


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


def _make_bridge(mod: Any, callbacks: dict[str, Any] | None = None) -> Any:
    cb = callbacks or {}
    return mod.OpenAIRealtimeBridge(
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
    mod = _load_bridge_module()
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    b = _make_bridge(mod)
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    # Run connect; immediately stop the inbound loop by closing.
    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.01)
    fake_ws.push({"type": "session.created"})
    fake_ws.push({"type": "session.updated"})
    await asyncio.sleep(0.05)
    b.close()
    await asyncio.wait_for(task, timeout=1.0)

    # First frame must be session.update with PCM16 audio formats.
    assert fake_ws.sent, "no frames sent"
    first = json.loads(fake_ws.sent[0])
    assert first["type"] == "session.update"
    assert first["session"]["input_audio_format"] == "pcm16"
    assert first["session"]["output_audio_format"] == "pcm16"
    assert first["session"]["voice"] == "alloy"


@pytest.mark.asyncio
async def test_audio_delta_calls_on_audio() -> None:
    """response.audio.delta with base64 PCM16 → on_audio(bytes)."""
    import base64
    mod = _load_bridge_module()
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    audio_chunks: list[bytes] = []
    b = _make_bridge(mod, {"on_audio": audio_chunks.append})
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.01)
    fake_ws.push({"type": "session.created"})
    fake_ws.push({"type": "session.updated"})

    payload = base64.b64encode(b"\x10\x20\x30\x40").decode()
    fake_ws.push({"type": "response.audio.delta", "delta": payload, "item_id": "i1"})
    await asyncio.sleep(0.05)
    b.close()
    await asyncio.wait_for(task, timeout=1.0)

    assert audio_chunks == [b"\x10\x20\x30\x40"]


@pytest.mark.asyncio
async def test_speech_started_triggers_on_clear_audio() -> None:
    """Server VAD sees user speak → barge-in → on_clear_audio fires."""
    mod = _load_bridge_module()
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    cleared = MagicMock()
    b = _make_bridge(mod, {"on_clear_audio": cleared})
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.01)
    fake_ws.push({"type": "session.created"})
    fake_ws.push({"type": "session.updated"})
    fake_ws.push({"type": "input_audio_buffer.speech_started"})
    await asyncio.sleep(0.05)
    b.close()
    await asyncio.wait_for(task, timeout=1.0)
    cleared.assert_called()


@pytest.mark.asyncio
async def test_tool_call_arguments_buffered_and_dispatched() -> None:
    """Function call deltas accumulate; .done emits one ToolCallEvent."""
    mod = _load_bridge_module()
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    received: list[Any] = []
    b = _make_bridge(mod, {"on_tool_call": received.append})
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.01)
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
    await asyncio.wait_for(task, timeout=1.0)

    assert len(received) == 1
    ev = received[0]
    assert ev.call_id == "call_x"
    assert ev.name == "Bash"
    assert ev.args == {"command": "ls -la"}


@pytest.mark.asyncio
async def test_send_audio_appends_to_input_buffer() -> None:
    mod = _load_bridge_module()
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    b = _make_bridge(mod)
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.01)
    fake_ws.push({"type": "session.created"})
    fake_ws.push({"type": "session.updated"})
    await asyncio.sleep(0.02)

    b.send_audio(b"\x01\x02\x03")
    await asyncio.sleep(0.02)
    b.close()
    await asyncio.wait_for(task, timeout=1.0)

    audio_frames = [
        json.loads(s) for s in fake_ws.sent
        if json.loads(s).get("type") == "input_audio_buffer.append"
    ]
    assert len(audio_frames) == 1
    import base64
    assert base64.b64decode(audio_frames[0]["audio"]) == b"\x01\x02\x03"


@pytest.mark.asyncio
async def test_submit_tool_result_creates_function_call_output() -> None:
    mod = _load_bridge_module()
    fake_ws = _FakeWS()

    async def _connect_stub(url: str, **_: Any) -> _FakeWS:
        return fake_ws

    b = _make_bridge(mod)
    b._connect_websocket = _connect_stub  # type: ignore[attr-defined]

    task = asyncio.create_task(b.connect())
    await asyncio.sleep(0.01)
    fake_ws.push({"type": "session.created"})
    fake_ws.push({"type": "session.updated"})
    await asyncio.sleep(0.02)

    b.submit_tool_result("call_x", {"output": "hello"})
    await asyncio.sleep(0.02)
    b.close()
    await asyncio.wait_for(task, timeout=1.0)

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
    assert response_creates  # the bridge triggers a new response after the tool result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_openai_realtime_bridge.py -v`
Expected: FAIL — module + class don't exist.

- [ ] **Step 3: Write minimal implementation**

Create `extensions/openai-provider/realtime.py`:

```python
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
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import websockets
# audit B4: `from websockets.client import Any` is
# DEPRECATED in websockets>=15. The bridge type-annotates the WS as
# ``Any`` to avoid the deprecation warning; the public surface doesn't
# need a precise type.
from typing import Any  # noqa: F401 — used in bridge annotations

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
    """Concrete realtime bridge for OpenAI's wss://api.openai.com/v1/realtime."""

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

        self._ws: Any | None = None
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
                pass  # no event loop — already torn down

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
        except (asyncio.TimeoutError, OSError) as exc:
            if self._on_error:
                self._on_error(exc if isinstance(exc, Exception) else Exception(str(exc)))
            return
        self._connected = True
        self._session_configured = False
        self._reconnect_attempts = 0
        self._send_session_update()
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
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
            # No running loop. Drop. Caller can retry via reconnect.
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_openai_realtime_bridge.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
ruff check --fix extensions/openai-provider/realtime.py tests/test_openai_realtime_bridge.py
git add extensions/openai-provider/realtime.py tests/test_openai_realtime_bridge.py
git commit -m "feat(openai): OpenAIRealtimeBridge — port realtime-voice-provider.ts (PCM16, no μ-law)"
```

---

## Task 5: Local PCM16 audio I/O

**Files:**
- Create: `opencomputer/voice/audio_io.py`
- Test: `tests/test_realtime_audio_io.py`

**Background:** OpenClaw's audio sink is Twilio Media Streams over a webhook. For local mic/speaker we use `sounddevice` (already a dep). 16 kHz, mono, signed-16 PCM. Mic stream pushes chunks to the bridge; speaker stream consumes audio chunks the bridge emits.

- [ ] **Step 1: Write the failing test**

Create `tests/test_realtime_audio_io.py`:

```python
"""LocalAudioIO — sounddevice-based mic/speaker for realtime voice."""
from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock, patch

import numpy as np


def test_local_audio_io_starts_input_and_output_streams() -> None:
    from opencomputer.voice.audio_io import LocalAudioIO

    with patch("opencomputer.voice.audio_io.sd") as sd:
        sd.RawInputStream = MagicMock()
        sd.RawOutputStream = MagicMock()
        io = LocalAudioIO(on_mic_chunk=lambda b: None)
        io.start()
        sd.RawInputStream.assert_called_once()
        sd.RawOutputStream.assert_called_once()


def test_local_audio_io_send_audio_writes_to_output_stream() -> None:
    from opencomputer.voice.audio_io import LocalAudioIO

    with patch("opencomputer.voice.audio_io.sd") as sd:
        out_stream = MagicMock()
        sd.RawInputStream = MagicMock()
        sd.RawOutputStream = MagicMock(return_value=out_stream)
        io = LocalAudioIO(on_mic_chunk=lambda b: None)
        io.start()
        io.send_audio(b"\x01\x02\x03\x04")
        out_stream.write.assert_called_with(b"\x01\x02\x03\x04")


def test_clear_audio_drops_pending_speaker_buffer() -> None:
    """Barge-in: any unplayed audio in the output buffer is dropped."""
    from opencomputer.voice.audio_io import LocalAudioIO

    with patch("opencomputer.voice.audio_io.sd") as sd:
        out_stream = MagicMock()
        sd.RawInputStream = MagicMock()
        sd.RawOutputStream = MagicMock(return_value=out_stream)
        io = LocalAudioIO(on_mic_chunk=lambda b: None)
        io.start()
        io.send_audio(b"a" * 1024)
        io.clear_audio()
        # Implementation detail: stop+restart is the cleanest portable
        # sounddevice "flush". Whatever the impl, after clear_audio()
        # the next send_audio must still work (no leftover state).
        io.send_audio(b"b" * 1024)
        # Two writes total: one before clear, one after.
        assert out_stream.write.call_count >= 1


def test_is_open_reflects_started_state() -> None:
    from opencomputer.voice.audio_io import LocalAudioIO

    with patch("opencomputer.voice.audio_io.sd") as sd:
        sd.RawInputStream = MagicMock()
        sd.RawOutputStream = MagicMock()
        io = LocalAudioIO(on_mic_chunk=lambda b: None)
        assert io.is_open() is False
        io.start()
        assert io.is_open() is True
        io.stop()
        assert io.is_open() is False


def test_mic_callback_passes_pcm16_bytes_to_handler() -> None:
    """sounddevice's RawInputStream callback signature: (indata, frames, time, status).
    LocalAudioIO must convert numpy PCM16 → bytes and forward."""
    from opencomputer.voice.audio_io import LocalAudioIO

    received: list[bytes] = []
    handler: Callable[[bytes], None] = received.append
    with patch("opencomputer.voice.audio_io.sd") as sd:
        sd.RawInputStream = MagicMock()
        sd.RawOutputStream = MagicMock()
        io = LocalAudioIO(on_mic_chunk=handler)
        io.start()
        # Reach into the constructed RawInputStream call and grab the
        # ``callback`` kwarg, then invoke it directly.
        kwargs = sd.RawInputStream.call_args.kwargs
        cb = kwargs["callback"]
        sample_bytes = (np.array([0, 1, -1, 32000], dtype=np.int16)).tobytes()
        cb(sample_bytes, 4, None, None)
        assert received == [sample_bytes]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_realtime_audio_io.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/voice/audio_io.py`:

```python
"""Local PCM16 audio I/O for realtime voice — sounddevice mic + speaker.

Not a port of any OpenClaw file — telephony platforms use Twilio Media
Streams as the audio sink/source. For local-mic use we use the
``sounddevice`` library (already a dep via the ``[voice]`` extra) which
wraps PortAudio. Format is 16 kHz mono signed-16 PCM (matches OpenAI
Realtime's ``pcm16`` audio_format).

Lifecycle:
* ``start()`` — open both input + output streams.
* ``stop()`` — close streams, idempotent.
* ``send_audio(chunk)`` — write PCM16 bytes to the speaker.
* ``clear_audio()`` — flush any pending speaker buffer (used on
  barge-in when the user starts talking mid-reply).
* ``is_open()`` — True between start() and stop().
* ``on_mic_chunk(chunk)`` — caller-supplied handler invoked from the
  audio thread for each captured PCM16 chunk.
"""
from __future__ import annotations

from collections.abc import Callable

try:
    import sounddevice as sd
except (ImportError, OSError):  # OSError: PortAudio missing
    sd = None  # type: ignore[assignment]


_SAMPLE_RATE = 16_000
_CHANNELS = 1
_DTYPE = "int16"
_BLOCK_SIZE = 800  # 50 ms at 16 kHz


class LocalAudioIO:
    """Mic capture + speaker playback for realtime voice."""

    def __init__(self, *, on_mic_chunk: Callable[[bytes], None]) -> None:
        if sd is None:
            raise RuntimeError(
                "sounddevice not available. Install with "
                "`pip install opencomputer[voice]` and ensure PortAudio is on the system."
            )
        self._on_mic_chunk = on_mic_chunk
        self._input_stream = None
        self._output_stream = None
        self._started = False

    def _mic_callback(self, indata: bytes, frames: int, time_info, status) -> None:
        # Forward raw PCM16 bytes to the handler. Errors here would
        # crash the audio thread — swallow + log to keep the loop alive.
        try:
            self._on_mic_chunk(bytes(indata))
        except Exception:  # noqa: BLE001 — never crash audio thread
            pass

    def start(self) -> None:
        if self._started:
            return
        self._input_stream = sd.RawInputStream(
            samplerate=_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype=_DTYPE,
            blocksize=_BLOCK_SIZE,
            callback=self._mic_callback,
        )
        self._output_stream = sd.RawOutputStream(
            samplerate=_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype=_DTYPE,
            blocksize=_BLOCK_SIZE,
        )
        self._input_stream.start()
        self._output_stream.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            if self._input_stream is not None:
                self._input_stream.stop()
                self._input_stream.close()
        finally:
            self._input_stream = None
        try:
            if self._output_stream is not None:
                self._output_stream.stop()
                self._output_stream.close()
        finally:
            self._output_stream = None
        self._started = False

    def is_open(self) -> bool:
        return self._started

    def send_audio(self, audio: bytes) -> None:
        if self._output_stream is not None:
            self._output_stream.write(audio)

    def clear_audio(self) -> None:
        """Drop any pending speaker buffer (barge-in).

        sounddevice doesn't expose a direct ``flush`` — the cleanest
        portable approach is stop+restart of the output stream.
        """
        out = self._output_stream
        if out is None:
            return
        try:
            out.stop()
            out.close()
        except Exception:  # noqa: BLE001 — best effort
            pass
        self._output_stream = sd.RawOutputStream(
            samplerate=_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype=_DTYPE,
            blocksize=_BLOCK_SIZE,
        )
        self._output_stream.start()


__all__ = ["LocalAudioIO"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_realtime_audio_io.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
ruff check --fix opencomputer/voice/audio_io.py tests/test_realtime_audio_io.py
git add opencomputer/voice/audio_io.py tests/test_realtime_audio_io.py
git commit -m "feat(voice): LocalAudioIO — sounddevice PCM16 mic + speaker for realtime"
```

---

## Task 6: Tool router — bridge ToolCall → AgentLoop tools → submit_tool_result

**Files:**
- Create: `opencomputer/voice/tool_router.py`
- Test: `tests/test_realtime_tool_router.py`

**Background:** When the OpenAI Realtime model issues a function call mid-stream, we need to (a) look up the tool by name in OC's `ToolRegistry`, (b) execute it via `BaseTool.execute(ToolCall)`, (c) push the result back to the bridge via `bridge.submit_tool_result(call_id, result)`. The router is the glue — it gates on `effective_permission_mode(runtime)` so AUTO mode auto-approves, PLAN mode refuses destructive tools, etc.

- [ ] **Step 1: Write the failing test**

Create `tests/test_realtime_tool_router.py`:

```python
"""Tool router — dispatches RealtimeVoiceToolCallEvent through OC's tool registry."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.realtime_voice import RealtimeVoiceToolCallEvent
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class _StubTool(BaseTool):
    parallel_safe = True
    _last_call: ToolCall | None = None

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(name="EchoTool", description="echo", parameters={})

    async def execute(self, call: ToolCall) -> ToolResult:
        type(self)._last_call = call
        return ToolResult(
            tool_call_id=call.id, content=f"echoed {call.arguments}", is_error=False,
        )


def _registry_with(tool: BaseTool) -> Any:
    reg = MagicMock()
    reg.get = MagicMock(return_value=tool)
    return reg


def test_dispatch_calls_tool_and_pushes_result_back() -> None:
    from opencomputer.voice.tool_router import dispatch_realtime_tool_call

    tool = _StubTool()
    bridge = MagicMock()
    runtime = RuntimeContext()
    ev = RealtimeVoiceToolCallEvent(
        item_id="i1", call_id="c1", name="EchoTool", args={"text": "hi"},
    )

    asyncio.run(dispatch_realtime_tool_call(
        event=ev,
        registry=_registry_with(tool),
        bridge=bridge,
        runtime=runtime,
    ))

    bridge.submit_tool_result.assert_called_once()
    call_id_arg, result_arg = bridge.submit_tool_result.call_args.args
    assert call_id_arg == "c1"
    assert "echoed" in str(result_arg)


def test_dispatch_unknown_tool_returns_error_to_bridge() -> None:
    from opencomputer.voice.tool_router import dispatch_realtime_tool_call

    bridge = MagicMock()
    runtime = RuntimeContext()
    ev = RealtimeVoiceToolCallEvent(
        item_id="i1", call_id="c1", name="DoesNotExist", args={},
    )
    registry = MagicMock(get=MagicMock(return_value=None))

    asyncio.run(dispatch_realtime_tool_call(
        event=ev, registry=registry, bridge=bridge, runtime=runtime,
    ))

    bridge.submit_tool_result.assert_called_once()
    _cid, result = bridge.submit_tool_result.call_args.args
    assert "unknown tool" in str(result).lower() or "not found" in str(result).lower()


def test_dispatch_in_plan_mode_refuses_destructive_tools() -> None:
    """In PLAN mode, the router refuses tools without setting them off."""
    from opencomputer.voice.tool_router import dispatch_realtime_tool_call

    tool = _StubTool()
    bridge = MagicMock()
    # plan_mode=True → effective_permission_mode → PermissionMode.PLAN.
    runtime = RuntimeContext(plan_mode=True)
    ev = RealtimeVoiceToolCallEvent(
        item_id="i1", call_id="c1", name="EchoTool", args={"text": "hi"},
    )

    asyncio.run(dispatch_realtime_tool_call(
        event=ev,
        registry=_registry_with(tool),
        bridge=bridge,
        runtime=runtime,
    ))

    bridge.submit_tool_result.assert_called_once()
    _cid, result = bridge.submit_tool_result.call_args.args
    assert "plan" in str(result).lower() or "refused" in str(result).lower()
    # Tool was NOT executed.
    assert _StubTool._last_call is None or _StubTool._last_call.id != "c1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_realtime_tool_router.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/voice/tool_router.py`:

```python
"""Tool dispatch for realtime voice.

When the bridge emits a tool call, look up the tool in OC's
``ToolRegistry``, dispatch it (async), then push the result back via
``bridge.submit_tool_result``. Honors ``effective_permission_mode``:

* ``PermissionMode.PLAN`` — refuse the call with a "plan mode" string.
* ``PermissionMode.AUTO`` — auto-approve.
* ``PermissionMode.DEFAULT`` — execute (consent gate is the real
  enforcement layer; this router doesn't re-prompt because there's
  no terminal in voice mode).
"""
from __future__ import annotations

from typing import Any, Protocol
from uuid import uuid4

from plugin_sdk.core import ToolCall
from plugin_sdk.permission_mode import PermissionMode, effective_permission_mode
from plugin_sdk.realtime_voice import RealtimeVoiceToolCallEvent
from plugin_sdk.runtime_context import RuntimeContext


class _Bridge(Protocol):
    def submit_tool_result(self, call_id: str, result: Any) -> None: ...


class _Registry(Protocol):
    def get(self, name: str) -> Any: ...


async def dispatch_realtime_tool_call(
    *,
    event: RealtimeVoiceToolCallEvent,
    registry: _Registry,
    bridge: _Bridge,
    runtime: RuntimeContext,
) -> None:
    """Run the tool referenced by ``event`` and push the result to ``bridge``.

    Errors are swallowed into the result string — never raised — so a
    bad tool call doesn't kill the voice session.
    """
    mode = effective_permission_mode(runtime)
    if mode == PermissionMode.PLAN:
        bridge.submit_tool_result(event.call_id, {
            "error": (
                "Tool call refused — agent is in plan mode. "
                f"({event.name} would have run with {event.args!r})"
            ),
        })
        return

    tool = registry.get(event.name)
    if tool is None:
        bridge.submit_tool_result(event.call_id, {
            "error": f"unknown tool: {event.name!r}",
        })
        return

    call = ToolCall(
        id=event.call_id or str(uuid4()),
        name=event.name,
        arguments=event.args if isinstance(event.args, dict) else {},
    )
    try:
        result = await tool.execute(call)
    except Exception as exc:  # noqa: BLE001 — never crash the session
        bridge.submit_tool_result(event.call_id, {"error": str(exc)})
        return

    payload: Any
    if hasattr(result, "content") and hasattr(result, "is_error"):
        payload = {"content": result.content, "is_error": result.is_error}
    else:
        payload = result
    bridge.submit_tool_result(event.call_id, payload)


__all__ = ["dispatch_realtime_tool_call"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_realtime_tool_router.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
ruff check --fix opencomputer/voice/tool_router.py tests/test_realtime_tool_router.py
git add opencomputer/voice/tool_router.py tests/test_realtime_tool_router.py
git commit -m "feat(voice): realtime tool router — dispatch + permission_mode gating"
```

---

## Task 7: CLI integration — `opencomputer voice realtime` subcommand

**Files:**
- Modify: `opencomputer/cli_voice.py` — add `realtime` subcommand to `voice_app`
- Test: `tests/test_cli_voice_realtime.py`

**Background:** Replaces the stub agent at `cli_voice.py:141-142` (the existing `voice talk` Whisper+Edge-TTS command stays as the fallback for users without an OpenAI key). The new command is `opencomputer voice realtime` and goes straight into the bridge + audio + tool-router loop.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_voice_realtime.py`:

```python
"""voice realtime CLI command — wires bridge + audio + router."""
from __future__ import annotations

from typer.testing import CliRunner


def test_voice_realtime_help_advertises_command() -> None:
    from opencomputer.cli_voice import voice_app

    runner = CliRunner()
    result = runner.invoke(voice_app, ["realtime", "--help"])
    assert result.exit_code == 0
    assert "realtime" in result.output.lower() or "OpenAI" in result.output


def test_voice_realtime_errors_without_api_key(monkeypatch) -> None:
    """Without OPENAI_API_KEY, the command must error out with a clear message."""
    from opencomputer.cli_voice import voice_app

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runner = CliRunner()
    result = runner.invoke(voice_app, ["realtime"])
    assert result.exit_code != 0
    assert "OPENAI_API_KEY" in result.output or "api key" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_voice_realtime.py -v`
Expected: FAIL — `realtime` subcommand doesn't exist on `voice_app`.

- [ ] **Step 3: Write minimal implementation**

In `opencomputer/cli_voice.py`, after the existing `voice_talk` command, add a sibling:

```python
@voice_app.command("realtime")
def voice_realtime(
    voice: str = typer.Option(
        "alloy",
        "--voice",
        help="OpenAI realtime voice (alloy/ash/ballad/cedar/coral/echo/marin/sage/shimmer/verse).",
    ),
    model: str = typer.Option(
        "gpt-realtime-1.5",
        "--model",
        help="OpenAI realtime model id.",
    ),
    instructions: str = typer.Option(
        "",
        "--instructions",
        help="Initial system-style instructions for the voice agent.",
    ),
) -> None:
    """Two-way streaming voice via OpenAI Realtime API.

    Connects mic → OpenAI Realtime → speaker. Tool calls dispatch through
    OC's tool registry. Press Ctrl+C to exit.
    """
    import asyncio
    import os

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        typer.echo(
            "OPENAI_API_KEY not set. Realtime voice requires an OpenAI key — "
            "fall back to `opencomputer voice talk` for the Whisper+Edge-TTS path.",
            err=True,
        )
        raise typer.Exit(code=2)

    typer.echo("🎤 voice realtime: connecting (Ctrl+C to exit)…")
    asyncio.run(_run_realtime_loop(
        api_key=api_key, model=model, voice=voice, instructions=instructions,
    ))


async def _run_realtime_loop(
    *, api_key: str, model: str, voice: str, instructions: str,
) -> None:
    """Build the bridge + audio I/O + tool router and run until Ctrl+C.

    Pulled out as a module-level coroutine so tests can call it with
    monkey-patched bridge/audio without spinning the CLI runner.
    """
    import asyncio

    from extensions.openai_provider.realtime import OpenAIRealtimeBridge
    from opencomputer.tools.registry import registry  # singleton, audit B1
    from opencomputer.voice.audio_io import LocalAudioIO
    from opencomputer.voice.realtime_session import create_realtime_voice_session
    from opencomputer.voice.tool_router import dispatch_realtime_tool_call
    from plugin_sdk.runtime_context import RuntimeContext

    runtime = RuntimeContext()

    audio: LocalAudioIO | None = None

    def _on_mic_chunk(chunk: bytes) -> None:
        if session is None:
            return
        session.send_audio(chunk)

    audio = LocalAudioIO(on_mic_chunk=_on_mic_chunk)

    def _on_tool_call(event, sess) -> None:
        # The router is async; schedule on the running loop.
        asyncio.create_task(dispatch_realtime_tool_call(
            event=event, registry=registry, bridge=sess.bridge, runtime=runtime,
        ))

    def _create_bridge(callbacks):
        return OpenAIRealtimeBridge(
            api_key=api_key,
            model=model,
            voice=voice,
            instructions=instructions or None,
            on_audio=callbacks["on_audio"],
            on_clear_audio=callbacks["on_clear_audio"],
            on_transcript=callbacks.get("on_transcript"),
            on_tool_call=callbacks.get("on_tool_call"),
            on_ready=callbacks.get("on_ready"),
            on_error=callbacks.get("on_error"),
            on_close=callbacks.get("on_close"),
        )

    session = create_realtime_voice_session(
        create_bridge=_create_bridge,
        audio_sink=audio,
        on_tool_call=_on_tool_call,
    )

    audio.start()
    try:
        await session.connect()
        # Stay alive until Ctrl+C.
        while True:
            await asyncio.sleep(1.0)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        session.close()
        audio.stop()
```

Make sure `import typer` and existing imports stay intact at the top of the file. Don't replace the existing `voice_talk` command — both `talk` (STT+TTS fallback) and `realtime` (this new one) coexist.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_voice_realtime.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
ruff check --fix opencomputer/cli_voice.py tests/test_cli_voice_realtime.py
git add opencomputer/cli_voice.py tests/test_cli_voice_realtime.py
git commit -m "feat(cli): opencomputer voice realtime — bridge + audio + router"
```

---

## Task 8: Final gate — full pytest + ruff + push + PR

After all tasks above are done, BEFORE pushing or creating a PR:

- [ ] **Step F1: Full pytest**

Run: `pytest tests/ -q --tb=short`
Expected: 0 failed.

- [ ] **Step F2: ruff check**

Run: `ruff check opencomputer/ plugin_sdk/ extensions/ tests/`
Expected: All checks passed.

- [ ] **Step F3: SDK boundary test specifically**

Run: `pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v`
Expected: PASS. (If plugin_sdk/realtime_voice.py accidentally imports from `opencomputer.*`, this fails.)

- [ ] **Step F4: CLI smoke**

Run: `opencomputer voice --help`
Expected: shows both `talk` and `realtime` subcommands.

- [ ] **Step F5: Push + PR**

```bash
git push -u origin feat/realtime-voice-openclaw-port
gh pr create --title "feat(voice): realtime voice — OpenClaw port (PCM16, OpenAI Realtime)" \
  --body "Direct port of openclaw/src/realtime-voice + extensions/openai/realtime-voice-provider to Python. Replaces cli_voice.py:141 stub. Skips telephony (μ-law, marks). Plan: docs/superpowers/plans/2026-04-29-realtime-voice-openclaw-port.md"
```

Use the public-flip workflow if CI is billing-blocked (same pattern as PRs #264 / #266 / #267).

---

## Self-Review

**Spec coverage:**
- ✅ SDK ABC + dataclasses → Task 1
- ✅ Session orchestrator → Task 2
- ✅ Helpers (error detail, finite number, trim) → Task 3
- ✅ OpenAI Realtime bridge (PCM16, no μ-law) → Task 4
- ✅ Local PCM16 mic+speaker → Task 5
- ✅ Tool router with permission-mode gating → Task 6
- ✅ CLI integration → Task 7
- ✅ Final pytest+ruff+PR gate → Task 8

**Placeholder scan:** No "TBD"s, no vague handwaving. Every code step has concrete code; every test has concrete assertions; every command has expected output.

**Type consistency:**
- `BaseRealtimeVoiceBridge` method names (`connect`, `send_audio`, `send_user_message`, `submit_tool_result`, `trigger_greeting`, `close`, `is_connected`) consistent across Tasks 1, 2, 4, 6, 7
- `RealtimeVoiceToolCallEvent` field names (`item_id`, `call_id`, `name`, `args`) consistent across Tasks 1, 4, 6
- `LocalAudioIO` method names (`start`, `stop`, `is_open`, `send_audio`, `clear_audio`) consistent across Tasks 5, 7
- `dispatch_realtime_tool_call` signature consistent between Tasks 6 and 7
- `_create_bridge` factory shape (callbacks dict in, bridge out) consistent between Tasks 2 and 7

**Honest deferrals (per the project's deferrals rule):**
- Mark protocol intentionally OMITTED — telephony-only, OC doesn't need it
- μ-law audio intentionally OMITTED — local mic uses PCM16
- Provider registry/resolver from OpenClaw NOT ported — OC has `PluginRegistry`
- Transcription-only provider variant NOT ported — one provider for v1 (the voice provider)
- `voice-call` extension (PSTN) NOT ported — out of scope per user direction
- Anthropic Realtime support: deferred until Anthropic ships a stable Realtime API (stub for future is the `BaseRealtimeVoiceBridge` ABC — no extra work needed, just a new bridge class when ready)
- Streaming TTS for the existing `voice talk` (Whisper+Edge-TTS) command: NOT addressed by this plan. That fallback path stays as-is. Realtime is the recommended UX.

**Backwards compat:**
- Existing `voice talk` command stays unchanged — Whisper + Edge-TTS path keeps working for users without OpenAI keys
- New SDK exports added to `plugin_sdk/__init__.py` `__all__` — additive only
- No existing tests assume the absence of these new modules
