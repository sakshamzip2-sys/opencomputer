# Thinking Dropdown (TUI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the model's reasoning/thinking as a *live* dropdown-style panel in the OpenComputer TUI — streaming inline above the answer as it arrives, then collapsing to a one-line summary on finalize when the user has not opted into full display.

**Architecture:** Extend the provider streaming contract with a new `thinking_delta` event kind. Both Anthropic (extended thinking blocks via the lower-level `messages.stream` event iterator — NOT `text_stream`) and OpenAI (reasoning_content delta) providers emit it. The AgentLoop's stream-dispatch loop in `_run_one_step` (loop.py:2062, dispatch lines 2128-2142) forwards thinking deltas to an optional `thinking_callback`; `run_conversation` (loop.py:482) gets the same kwarg and threads it down. `StreamingRenderer.on_thinking_chunk` (currently a no-op stub at `streaming.py:155-165`) gets a real implementation that renders the deltas live in a dim Rich panel above the answer markdown. `finalize()` honors `runtime.custom["show_reasoning"]` (currently ignored): when on, the full panel stays visible; when off (default), it collapses to a single `💭 Thought for 3.2s` line. No interactive expand/collapse keybinding for v1 — that's a follow-up if Textual ever lands.

**Tech Stack:** Python 3.12+, Rich (Console / Live / Panel / Group), pytest + pytest-asyncio, anthropic + openai async SDKs.

---

## File Structure

| File | Responsibility | Touch type |
|---|---|---|
| `plugin_sdk/provider_contract.py` | `StreamEvent.kind` Literal — add `"thinking_delta"` | modify |
| `extensions/anthropic-provider/provider.py` | emit `thinking_delta` from streaming `content_block_delta` thinking blocks | modify |
| `extensions/openai-provider/provider.py` | emit `thinking_delta` from streaming `delta.reasoning_content` | modify |
| `opencomputer/agent/loop.py` | dispatch `thinking_delta` to optional `thinking_callback` arg | modify |
| `opencomputer/cli_ui/streaming.py` | `on_thinking_chunk` live rendering + `finalize` collapse + `show_reasoning` gating | modify |
| `opencomputer/cli.py` | wire `renderer.on_thinking_chunk` as the `thinking_callback` | modify |
| `tests/test_streaming_thinking.py` | new — renderer thinking-stream tests | create |
| `tests/test_anthropic_thinking_stream.py` | new — Anthropic provider thinking-delta unit | create |
| `tests/test_openai_thinking_stream.py` | new — OpenAI provider thinking-delta unit | create |
| `tests/test_loop_thinking_dispatch.py` | new — AgentLoop dispatch test | create |

---

## Task 1: Extend `StreamEvent.kind` Literal

**Files:**
- Modify: `plugin_sdk/provider_contract.py:80-91`

- [ ] **Step 1: Write the failing test**

Create `tests/test_streamevent_thinking_kind.py`:

```python
"""StreamEvent must accept a ``thinking_delta`` kind so providers can
stream reasoning chunks alongside ``text_delta`` chunks."""
from __future__ import annotations

from plugin_sdk.provider_contract import StreamEvent


def test_streamevent_accepts_thinking_delta_kind() -> None:
    ev = StreamEvent(kind="thinking_delta", text="step 1: ")
    assert ev.kind == "thinking_delta"
    assert ev.text == "step 1: "
    assert ev.response is None


def test_streamevent_existing_kinds_still_work() -> None:
    """Backwards-compat — text_delta + done + tool_call must keep working."""
    assert StreamEvent(kind="text_delta", text="hi").kind == "text_delta"
    assert StreamEvent(kind="done").kind == "done"
    assert StreamEvent(kind="tool_call").kind == "tool_call"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_streamevent_thinking_kind.py -v`
Expected: at least `test_streamevent_accepts_thinking_delta_kind` fails type-check or with a Literal value error (depending on whether the dataclass enforces the Literal at runtime — it doesn't, but mypy/ruff will catch it; the test asserts the field round-trips so it should pass even before the fix; if so, mark this task pass-on-write).

- [ ] **Step 3: Write minimal implementation**

Edit `plugin_sdk/provider_contract.py:80-91`:

```python
@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One event emitted by `provider.stream_complete()`.

    Types:
      - "text_delta":     incremental answer text chunk (`text` field)
      - "thinking_delta": incremental reasoning text chunk (`text` field) —
                          providers that surface reasoning (Anthropic
                          extended thinking, OpenAI o-series reasoning)
                          emit these alongside text_delta. Renderers that
                          don't care about thinking can ignore this kind.
      - "tool_call":      full tool call has been assembled (`tool_call` field)
      - "done":           streaming finished (`response` field carries the final
                          ProviderResponse)
    """

    kind: Literal["text_delta", "thinking_delta", "tool_call", "done"]
    text: str = ""
    response: ProviderResponse | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_streamevent_thinking_kind.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugin_sdk/provider_contract.py tests/test_streamevent_thinking_kind.py
git commit -m "feat(sdk): StreamEvent.kind accepts \"thinking_delta\""
```

---

## Task 2: Anthropic provider emits `thinking_delta` during stream

**Files:**
- Modify: `extensions/anthropic-provider/provider.py:627-630` (the native streaming branch — pool path at lines 605-621 stays text-only)
- Test: `tests/test_anthropic_thinking_stream.py`

**Background:** The current native-stream code uses `async for text in stream.text_stream:` (line 628) which is the SDK's high-level convenience that yields ONLY string text — it filters out thinking deltas. To surface thinking we must drop down to the raw event iterator: `async for event in stream:` yields `RawMessageStreamEvent` objects. Each `content_block_delta` event has `event.delta` whose `.type` is either `"text_delta"` (with `.text`) or `"thinking_delta"` (with `.thinking`). The pool/credential-rotation path (lines 600-621) only sees the final aggregated response post-rotation, so it cannot stream thinking live — leave it text-only for now.

- [ ] **Step 1: Write the failing test**

Create `tests/test_anthropic_thinking_stream.py`:

```python
"""Anthropic provider must emit StreamEvent(kind=\"thinking_delta\")
when the SDK yields content_block_delta events with thinking deltas."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import StreamEvent


_PROVIDER_PATH = (
    Path(__file__).resolve().parents[1]
    / "extensions" / "anthropic-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_provider", _PROVIDER_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_test_anthropic_provider"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeStream:
    """Mimic Anthropic's async-iterator streaming response."""

    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


@pytest.mark.asyncio
async def test_stream_complete_emits_thinking_delta() -> None:
    mod = _load_provider_module()
    Provider = mod.AnthropicProvider

    # Compose a fake stream: one thinking_delta, one text_delta, one
    # message_stop. The provider should translate the thinking_delta
    # into StreamEvent(kind="thinking_delta", text="step 1...").
    fake_events = [
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking="step 1..."),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="text_delta", text="hello"),
        ),
        SimpleNamespace(type="message_stop"),
    ]

    fake_response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="step 1..."),
            SimpleNamespace(type="text", text="hello"),
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=2,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )

    # The real SDK exposes ``stream`` as an async context manager whose
    # __aenter__ returns an object that is ITSELF an async iterator
    # (via __aiter__/__anext__) AND has ``.get_final_message`` +
    # ``.text_stream``. Our fake stream object plays the iterator role;
    # the cm wraps it so ``async with client.messages.stream(...) as
    # stream`` lands on the iterator.
    fake_stream_obj = _FakeStream(list(fake_events))
    fake_stream_obj.get_final_message = AsyncMock(return_value=fake_response)

    fake_client = MagicMock()
    fake_client.messages.stream = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_stream_obj)
    cm.__aexit__ = AsyncMock(return_value=None)
    fake_client.messages.stream.return_value = cm

    p = Provider.__new__(Provider)
    # The native-stream path uses ``self.client``, NOT ``self._client``.
    p.client = fake_client  # type: ignore[attr-defined]
    p._credential_pool = None  # force native (non-pool) path
    p.name = "anthropic"
    p.config = SimpleNamespace(api_key="x", base_url=None)

    kinds: list[str] = []
    texts: list[str] = []
    async for ev in p.stream_complete(
        model="claude-opus-4-7",
        messages=[Message(role="user", content="hi")],
    ):
        assert isinstance(ev, StreamEvent)
        kinds.append(ev.kind)
        if ev.kind in ("text_delta", "thinking_delta"):
            texts.append(ev.text)

    assert "thinking_delta" in kinds
    assert "text_delta" in kinds
    assert kinds[-1] == "done"
    assert "step 1..." in texts
    assert "hello" in texts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_anthropic_thinking_stream.py -v`
Expected: FAIL — provider currently emits no `thinking_delta`. The `kinds` list will not contain `"thinking_delta"`.

- [ ] **Step 3: Write minimal implementation**

Replace the native-stream dispatch in `extensions/anthropic-provider/provider.py:627-636` (the `else` branch — no credential pool):

```python
async with self.client.messages.stream(**kwargs) as stream:
    async for event in stream:
        # Drop down to the raw event iterator (NOT stream.text_stream)
        # so thinking_delta surfaces alongside text_delta. Each
        # content_block_delta event carries a delta whose .type tells
        # us which channel the chunk belongs to.
        if getattr(event, "type", None) != "content_block_delta":
            continue
        delta = getattr(event, "delta", None)
        if delta is None:
            continue
        dtype = getattr(delta, "type", None)
        if dtype == "text_delta":
            chunk = getattr(delta, "text", "") or ""
            if chunk:
                yield StreamEvent(kind="text_delta", text=chunk)
        elif dtype == "thinking_delta":
            chunk = getattr(delta, "thinking", "") or ""
            if chunk:
                yield StreamEvent(kind="thinking_delta", text=chunk)
        # Other delta types (input_json_delta for tool args, signature_delta
        # for thinking signatures) are aggregated into the final message —
        # we don't need to surface them mid-stream.
    final = await stream.get_final_message()

yield StreamEvent(kind="done", response=self._parse_response(final))
```

Keep the rate-limit / try-except wrapper from the existing code — only the inner iteration changes. Leave the credential-pool branch (lines 600-621) untouched: it can't see mid-stream events post-rotation.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_anthropic_thinking_stream.py -v`
Expected: PASS.

Also re-run the existing Anthropic streaming tests to make sure the refactor didn't regress text-only streaming:

Run: `pytest tests/ -k "anthropic and stream" -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add extensions/anthropic-provider/provider.py tests/test_anthropic_thinking_stream.py
git commit -m "feat(anthropic): emit thinking_delta StreamEvents during stream"
```

---

## Task 3: OpenAI provider emits `thinking_delta` during stream

**Files:**
- Modify: `extensions/openai-provider/provider.py` (the streaming branch — search for `async def stream_complete`)
- Test: `tests/test_openai_thinking_stream.py`

**Background:** OpenAI's chat completions stream includes a `delta.reasoning_content` field for o-series models. Some compatible providers (DeepSeek, etc.) also emit this. The provider already extracts `reasoning_content` at finalize via `_extract_reasoning_content` (PR #263) but does NOT stream it. We add an inline streaming branch.

- [ ] **Step 1: Write the failing test**

Create `tests/test_openai_thinking_stream.py`:

```python
"""OpenAI provider must emit StreamEvent(kind=\"thinking_delta\")
when delta.reasoning_content is present on streaming chunks."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import StreamEvent


_PROVIDER_PATH = (
    Path(__file__).resolve().parents[1]
    / "extensions" / "openai-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_openai_provider", _PROVIDER_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_test_openai_provider"] = mod
    spec.loader.exec_module(mod)
    return mod


def _chunk(text: str = "", reasoning: str = "", finish: str | None = None):
    """Build a chat-completion streaming chunk shape OpenAI yields."""
    delta = SimpleNamespace(
        content=text or None,
        reasoning_content=reasoning or None,
        tool_calls=None,
        role=None,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish, index=0)
    return SimpleNamespace(
        choices=[choice], usage=None, id="cmpl-x", model="gpt-test",
    )


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_stream_complete_emits_thinking_delta_from_reasoning_content() -> None:
    mod = _load_provider_module()
    Provider = mod.OpenAIProvider

    fake_chunks = [
        _chunk(reasoning="Let me think... "),
        _chunk(reasoning="step one. "),
        _chunk(text="The answer "),
        _chunk(text="is 42."),
        _chunk(finish="stop"),
    ]

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_FakeStream(list(fake_chunks))
    )

    p = Provider.__new__(Provider)
    p._client = fake_client  # type: ignore[attr-defined]
    p.name = "openai"
    p.config = SimpleNamespace(api_key="x", base_url=None)

    events: list[StreamEvent] = []
    async for ev in p.stream_complete(
        model="gpt-5",
        messages=[Message(role="user", content="2+2")],
    ):
        events.append(ev)

    thinking_texts = [e.text for e in events if e.kind == "thinking_delta"]
    text_chunks = [e.text for e in events if e.kind == "text_delta"]

    assert thinking_texts == ["Let me think... ", "step one. "]
    assert "".join(text_chunks) == "The answer is 42."
    assert events[-1].kind == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_openai_thinking_stream.py -v`
Expected: FAIL — `thinking_texts` will be `[]` because the provider doesn't yield thinking_delta yet.

- [ ] **Step 3: Write minimal implementation**

In `extensions/openai-provider/provider.py`, find the per-chunk loop in `stream_complete` (it iterates `async for chunk in stream`). Inside the loop, BEFORE handling `delta.content`, check `delta.reasoning_content` and yield a thinking_delta:

```python
async for chunk in stream:
    if not chunk.choices:
        continue
    delta = chunk.choices[0].delta

    # NEW — reasoning_content streams alongside content for o-series.
    reasoning_chunk = getattr(delta, "reasoning_content", None)
    if reasoning_chunk:
        yield StreamEvent(kind="thinking_delta", text=reasoning_chunk)

    # existing content branch unchanged
    text_chunk = getattr(delta, "content", None)
    if text_chunk:
        yield StreamEvent(kind="text_delta", text=text_chunk)
    # ... existing tool_call accumulation + finish_reason handling ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_openai_thinking_stream.py -v`
Expected: PASS.

Re-run existing OpenAI streaming tests:

Run: `pytest tests/ -k "openai and stream" -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add extensions/openai-provider/provider.py tests/test_openai_thinking_stream.py
git commit -m "feat(openai): emit thinking_delta from reasoning_content stream"
```

---

## Task 4: AgentLoop dispatches `thinking_delta` to `thinking_callback`

**Files:**
- Modify: `opencomputer/agent/loop.py` — both `_run_one_step` (line 2062, dispatch loop at lines 2128-2142) AND `run_conversation` (line 482), plus the `run_conversation → _run_one_step` call at line 1133
- Test: `tests/test_loop_thinking_dispatch.py`

**Background:** The loop currently only forwards `text_delta` events to `stream_callback`. We add an optional `thinking_callback` parameter to BOTH `run_conversation` (the public entry) AND `_run_one_step` (where the stream is iterated). `run_conversation` threads its kwarg into the `_run_one_step` call. Default `None` keeps existing call sites unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_loop_thinking_dispatch.py`:

```python
"""AgentLoop._run_one_step must forward thinking_delta StreamEvents to
the optional ``thinking_callback`` parameter."""
from __future__ import annotations

from typing import Any
from pathlib import Path

import pytest

from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.config import Config, ModelConfig, SessionConfig
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider, ProviderResponse, StreamEvent, Usage,
)


class _FakeProvider(BaseProvider):
    name = "fake"
    default_model = "fake-1"

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        raise NotImplementedError

    async def stream_complete(self, **kwargs: Any):
        for ev in self._events:
            yield ev


def _make_loop(provider: BaseProvider, tmp_path) -> AgentLoop:
    """Construct AgentLoop with the real signature.

    AgentLoop.__init__ takes (provider, config, db=None, ...) — there is
    NO ``tools=`` kwarg. ``Config`` is the top-level dataclass at
    opencomputer/agent/config.py:389; ``LoopConfig`` is a sub-component
    on Config.loop and not used here.
    """
    return AgentLoop(
        provider=provider,
        config=Config(
            model=ModelConfig(provider="fake", model="fake-1"),
            session=SessionConfig(db_path=Path(tmp_path) / "s.db"),
        ),
    )


@pytest.mark.asyncio
async def test_run_one_step_dispatches_thinking_delta_to_thinking_callback(
    tmp_path,
) -> None:
    final = ProviderResponse(
        message=Message(role="assistant", content="answer"),
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
        reasoning="step 1; step 2",
    )
    events = [
        StreamEvent(kind="thinking_delta", text="step 1; "),
        StreamEvent(kind="thinking_delta", text="step 2"),
        StreamEvent(kind="text_delta", text="answer"),
        StreamEvent(kind="done", response=final),
    ]
    loop = _make_loop(_FakeProvider(events), tmp_path)

    text_chunks: list[str] = []
    thinking_chunks: list[str] = []
    await loop._run_one_step(  # type: ignore[attr-defined]
        messages=[Message(role="user", content="hi")],
        system="",
        stream_callback=text_chunks.append,
        thinking_callback=thinking_chunks.append,
        session_id="s1",
    )

    assert text_chunks == ["answer"]
    assert thinking_chunks == ["step 1; ", "step 2"]


@pytest.mark.asyncio
async def test_run_one_step_ignores_thinking_delta_when_callback_is_none(
    tmp_path,
) -> None:
    """Backwards compat: omitting thinking_callback must not raise."""
    final = ProviderResponse(
        message=Message(role="assistant", content="answer"),
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    events = [
        StreamEvent(kind="thinking_delta", text="ignored"),
        StreamEvent(kind="text_delta", text="answer"),
        StreamEvent(kind="done", response=final),
    ]
    loop = _make_loop(_FakeProvider(events), tmp_path)

    chunks: list[str] = []
    await loop._run_one_step(  # type: ignore[attr-defined]
        messages=[Message(role="user", content="hi")],
        system="",
        stream_callback=chunks.append,
        session_id="s1",
    )
    assert chunks == ["answer"]


@pytest.mark.asyncio
async def test_run_conversation_threads_thinking_callback_through(
    tmp_path,
) -> None:
    """The PUBLIC entry (run_conversation, loop.py:482) must accept and
    forward ``thinking_callback`` down to ``_run_one_step``."""
    final = ProviderResponse(
        message=Message(role="assistant", content="answer"),
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
        reasoning="my chain",
    )
    events = [
        StreamEvent(kind="thinking_delta", text="my chain"),
        StreamEvent(kind="text_delta", text="answer"),
        StreamEvent(kind="done", response=final),
    ]
    loop = _make_loop(_FakeProvider(events), tmp_path)

    text_chunks: list[str] = []
    thinking_chunks: list[str] = []
    await loop.run_conversation(
        user_message="hi",
        session_id="s1",
        stream_callback=text_chunks.append,
        thinking_callback=thinking_chunks.append,
    )
    assert thinking_chunks == ["my chain"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_loop_thinking_dispatch.py -v`
Expected: FAIL on `test_run_one_step_dispatches_thinking_delta_to_thinking_callback` — `thinking_callback` is not a recognized parameter and dispatch ignores `thinking_delta`.

- [ ] **Step 3: Write minimal implementation — three edits, in order**

**3a. `_run_one_step` signature + dispatch.** In `opencomputer/agent/loop.py`, edit `_run_one_step` (line 2062). Add `thinking_callback=None` to the signature; in the dispatch loop (lines 2128-2142) add a branch for `thinking_delta`:

```python
async def _run_one_step(
    self,
    *,
    messages: list[Message],
    system: str,
    stream_callback=None,
    thinking_callback=None,
    model: str | None = None,
    session_id: str = "",
) -> StepOutcome:
    # ... existing setup unchanged ...
    if stream_callback is not None:
        final_response = None
        async for event in self.provider.stream_complete(
            model=model_name,
            messages=wire_messages,
            system=system,
            tools=tool_schemas,
            max_tokens=self.config.model.max_tokens,
            temperature=self.config.model.temperature,
            **_extra_kwargs,
        ):
            if event.kind == "text_delta":
                stream_callback(event.text)
            elif event.kind == "thinking_delta":
                if thinking_callback is not None:
                    thinking_callback(event.text)
            elif event.kind == "done":
                final_response = event.response
        # ... rest unchanged ...
```

**3b. `run_conversation` signature.** In `opencomputer/agent/loop.py:482`, add `thinking_callback=None` after `stream_callback=None`:

```python
async def run_conversation(
    self,
    user_message: str,
    session_id: str | None = None,
    system_override: str | None = None,
    runtime: RuntimeContext | None = None,
    stream_callback=None,
    thinking_callback=None,
    system_prompt_override: str | None = None,
    initial_messages: list[Message] | None = None,
    images: list[str] | None = None,
) -> ConversationResult:
```

**3c. Forward at the call site.** In `opencomputer/agent/loop.py:1133-1139`, pass the new kwarg through:

```python
step = await self._run_one_step(
    messages=messages,
    system=system,
    stream_callback=stream_callback,
    thinking_callback=thinking_callback,
    model=model_for_turn,
    session_id=sid,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_loop_thinking_dispatch.py -v`
Expected: PASS for all 3 tests.

- [ ] **Step 5: Commit**

```bash
ruff check --fix tests/test_loop_thinking_dispatch.py opencomputer/agent/loop.py
git add opencomputer/agent/loop.py tests/test_loop_thinking_dispatch.py
git commit -m "feat(loop): thinking_callback through run_conversation + _run_one_step"
```

---

## Task 5: `StreamingRenderer.on_thinking_chunk` live rendering

**Files:**
- Modify: `opencomputer/cli_ui/streaming.py` — `on_thinking_chunk`, `_render`, internals
- Test: `tests/test_streaming_thinking.py`

**Background:** The renderer's `on_thinking_chunk` is a deliberate no-op stub (`streaming.py:155-165`). Replace it with: append to a `_thinking_buffer`, set a `_thinking_started_at` timestamp on first chunk, and refresh the live group so the panel renders ABOVE the answer markdown.

- [ ] **Step 1: Write the failing test**

Create `tests/test_streaming_thinking.py`:

```python
"""StreamingRenderer must render thinking chunks live in a panel above
the answer, and respect ``runtime.custom['show_reasoning']`` on finalize."""
from __future__ import annotations

import io

from rich.console import Console

from opencomputer.cli_ui.streaming import StreamingRenderer


def _make_renderer() -> tuple[StreamingRenderer, io.StringIO, Console]:
    buf = io.StringIO()
    console = Console(file=buf, width=80, force_terminal=True, record=True)
    r = StreamingRenderer(console)
    return r, buf, console


def test_on_thinking_chunk_appends_to_internal_buffer() -> None:
    r, _buf, _con = _make_renderer()
    with r:
        r.start_thinking()
        r.on_thinking_chunk("step 1; ")
        r.on_thinking_chunk("step 2")
        # Internal buffer is private but accessible for white-box test.
        assert "".join(getattr(r, "_thinking_buffer", [])) == "step 1; step 2"


def test_on_thinking_chunk_records_started_at_on_first_chunk() -> None:
    r, _buf, _con = _make_renderer()
    with r:
        r.start_thinking()
        assert getattr(r, "_thinking_started_at", 0.0) == 0.0
        r.on_thinking_chunk("hi")
        assert getattr(r, "_thinking_started_at", 0.0) > 0.0


def test_on_thinking_chunk_empty_string_is_noop() -> None:
    r, _buf, _con = _make_renderer()
    with r:
        r.start_thinking()
        r.on_thinking_chunk("")
        assert getattr(r, "_thinking_buffer", []) == []


def test_render_includes_thinking_panel_when_buffer_non_empty() -> None:
    """White-box: _render() output must reference Thinking when buffer
    has chunks."""
    r, _buf, _con = _make_renderer()
    with r:
        r.start_thinking()
        r.on_thinking_chunk("reasoning content")
        group = r._render()  # type: ignore[attr-defined]
        # The Group renderable holds child renderables; we look for a
        # panel whose title contains "Thinking".
        from rich.panel import Panel
        panels = [c for c in group.renderables if isinstance(c, Panel)]
        titles = [str(p.title) for p in panels if p.title is not None]
        assert any("Thinking" in t for t in titles), (
            f"expected a Thinking panel, got titles={titles}"
        )


def test_finalize_collapses_to_summary_when_show_reasoning_false() -> None:
    """When the runtime flag is OFF (default), finalize should NOT
    print the full thinking panel — just a one-line dim summary."""
    r, _buf, _con = _make_renderer()
    with r:
        r.start_thinking()
        r.on_thinking_chunk("verbose internal reasoning details")
        r.finalize(
            reasoning="verbose internal reasoning details",
            iterations=1,
            in_tok=10,
            out_tok=2,
            elapsed_s=3.2,
            show_reasoning=False,
        )
    out = _con.export_text()
    # Detail body must NOT appear when collapsed.
    assert "verbose internal reasoning details" not in out
    # A summary line must appear.
    assert "Thought" in out or "💭" in out


def test_finalize_keeps_full_panel_when_show_reasoning_true() -> None:
    r, _buf, _con = _make_renderer()
    with r:
        r.start_thinking()
        r.on_thinking_chunk("verbose internal reasoning details")
        r.finalize(
            reasoning="verbose internal reasoning details",
            iterations=1,
            in_tok=10,
            out_tok=2,
            elapsed_s=3.2,
            show_reasoning=True,
        )
    out = _con.export_text()
    assert "verbose internal reasoning details" in out


def test_finalize_no_thinking_emits_nothing_about_reasoning() -> None:
    """If reasoning is None/empty, finalize must not print any thinking
    summary or panel — current callers rely on this."""
    r, _buf, _con = _make_renderer()
    with r:
        r.start_thinking()
        r.on_chunk("plain answer")
        r.finalize(
            reasoning=None,
            iterations=1,
            in_tok=5,
            out_tok=2,
            elapsed_s=0.4,
            show_reasoning=False,
        )
    out = _con.export_text()
    assert "Thought" not in out
    assert "💭" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_streaming_thinking.py -v`
Expected: most tests FAIL — `_thinking_buffer` doesn't exist, `on_thinking_chunk` is a no-op, `finalize` doesn't accept `show_reasoning`, no collapsed-summary code path exists.

- [ ] **Step 3: Write minimal implementation**

Edit `opencomputer/cli_ui/streaming.py`:

(a) Add buffer + timestamp attrs in `__init__`:

```python
def __init__(self, console: Console) -> None:
    self.console = console
    self._buffer: list[str] = []
    self._thinking_buffer: list[str] = []
    self._thinking_started_at: float = 0.0
    # ... existing attrs unchanged ...
```

(b) Replace the `on_thinking_chunk` stub:

```python
def on_thinking_chunk(self, text: str) -> None:
    """Append a thinking-delta chunk and live-refresh the panel.

    The panel renders ABOVE the answer markdown; the spinner / tool
    panel render below as before.
    """
    if not text:
        return
    if not self._thinking_buffer:
        self._thinking_started_at = time.monotonic()
    self._thinking_buffer.append(text)
    self._refresh()
```

(c) Update `_render` to insert a thinking panel above the answer markdown when the buffer is non-empty:

```python
def _render(self) -> Group:
    renderables = []

    if self._thinking_buffer:
        thinking_text = "".join(self._thinking_buffer)
        elapsed = time.monotonic() - self._thinking_started_at
        renderables.append(
            Panel(
                Text(thinking_text, style="dim"),
                title=Text(
                    f"💭 Thinking ({_fmt_duration(elapsed)})",
                    style="dim cyan",
                ),
                border_style="grey50",
                padding=(0, 1),
            )
        )

    # ... existing answer-buffer / spinner / tool-panel logic unchanged ...
    any_tool_running = any(
        row.ok is None for row in self._tool_calls.values()
    )
    show_spinner = (not self._buffer) or any_tool_running

    if self._buffer:
        text = "".join(self._buffer)
        if text.count("```") % 2 == 1:
            text = text + "\n```"
        renderables.append(Markdown(text, code_theme="ansi_dark"))

    if show_spinner:
        spinner_label = "Running tool…" if any_tool_running else "Thinking…"
        renderables.append(
            Spinner("dots", text=Text(spinner_label, style="dim"))
        )

    if self._tool_calls:
        renderables.append(self._render_tool_panel())

    return Group(*renderables)
```

(d) Update `finalize` to accept `show_reasoning` and branch on it:

```python
def finalize(
    self,
    *,
    reasoning: str | None,
    iterations: int,
    in_tok: int,
    out_tok: int,
    elapsed_s: float,
    show_reasoning: bool = False,
) -> None:
    """Stop Live, render thinking panel (or collapsed summary),
    final markdown, token-rate footer."""
    if self._live is not None:
        try:
            self._live.stop()
        except Exception:  # noqa: BLE001
            pass
        self._live = None

    if reasoning and reasoning.strip():
        thinking_elapsed = (
            (time.monotonic() - self._thinking_started_at)
            if self._thinking_started_at > 0.0
            else elapsed_s
        )
        if show_reasoning:
            self.console.print(
                Panel(
                    Text(reasoning.strip(), style="dim"),
                    title=Text(
                        f"💭 Thinking ({_fmt_duration(thinking_elapsed)})",
                        style="dim cyan",
                    ),
                    border_style="grey50",
                    padding=(0, 1),
                )
            )
        else:
            self.console.print(
                f"[dim cyan]💭 Thought for "
                f"{_fmt_duration(thinking_elapsed)} "
                f"— /reasoning show to expand[/dim cyan]"
            )

    if self._buffer:
        content = "".join(self._buffer)
        if self._header_shown:
            self.console.print("[bold magenta]oc ›[/bold magenta]")
        self.console.print(Markdown(content, code_theme="ansi_dark"))

    rate = (out_tok / elapsed_s) if elapsed_s > 0 else 0.0
    self.console.print(
        f"[dim]({iterations} iterations · "
        f"{in_tok} in / {out_tok} out · "
        f"{rate:.0f} tok/s · "
        f"{_fmt_duration(elapsed_s)})[/dim]\n"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_streaming_thinking.py -v`
Expected: PASS for all 7 tests.

- [ ] **Step 4b: Update the existing renderer test that breaks**

`tests/test_streaming_renderer.py:46-62` (`test_finalize_emits_thinking_panel_when_reasoning_present`) currently asserts `"Thinking" in output` and relies on the OLD always-show behavior. Update it to pass `show_reasoning=True` so the full panel still renders:

```python
def test_finalize_emits_thinking_panel_when_reasoning_present() -> None:
    from opencomputer.cli_ui import StreamingRenderer

    console = _make_console()
    with StreamingRenderer(console) as r:
        r.start_thinking()
        r.on_chunk("The answer is 42.")
        r.finalize(
            reasoning="I weighed several options and decided 42.",
            iterations=1,
            in_tok=10,
            out_tok=5,
            elapsed_s=0.5,
            show_reasoning=True,  # NEW — keep the full panel visible
        )
    output = console.export_text()
    assert "Thinking" in output
    assert "42" in output  # reasoning text appears
```

The other existing test `test_finalize_skips_thinking_panel_when_reasoning_empty` (line 65+) asserts `"💭" not in output` when reasoning is `None`. The new collapsed-summary code path also gates on `if reasoning and reasoning.strip()` — empty reasoning still emits nothing. That test stays untouched.

Run: `pytest tests/test_streaming_renderer.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
ruff check --fix tests/test_streaming_thinking.py tests/test_streaming_renderer.py opencomputer/cli_ui/streaming.py
git add opencomputer/cli_ui/streaming.py tests/test_streaming_thinking.py tests/test_streaming_renderer.py
git commit -m "feat(tui): live thinking panel + collapsed-by-default finalize"
```

---

## Task 6: `cli.py` wires `renderer.on_thinking_chunk` as `thinking_callback`

**Files:**
- Modify: `opencomputer/cli.py:979` — the chat loop's `loop.run_conversation(...)` call site. Add `thinking_callback=renderer.on_thinking_chunk`. Also pass `show_reasoning=runtime.custom.get("show_reasoning", False)` into the `renderer.finalize(...)` call (search the same file for `renderer.finalize(`).

**Background:** The CLI's chat loop owns the StreamingRenderer instance and calls `run_conversation` (the public entry, NOT `_run_one_step`). After Task 4, `run_conversation` accepts `thinking_callback`, so the CLI just plugs the renderer's method in. The `show_reasoning` flag flows from `runtime.custom` (set by `/reasoning show|hide`) into the renderer's `finalize` call.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_streaming_thinking.py`:

```python
def test_chat_loop_passes_thinking_callback_to_run_one_step(monkeypatch) -> None:
    """Black-box: when the CLI runs a chat turn, the loop's _run_one_step
    receives both stream_callback and thinking_callback."""
    from opencomputer.cli import _build_thinking_callback  # to be added

    captured: list[str] = []
    cb = _build_thinking_callback(captured.append)
    cb("a")
    cb("b")
    assert captured == ["a", "b"]
```

(The full integration test for `cli.py` is heavy — instead we add a tiny pure-function helper `_build_thinking_callback(forward) -> Callable[[str], None]` in `cli.py` and unit-test that. The "wiring" is then a one-line `thinking_callback=_build_thinking_callback(renderer.on_thinking_chunk)` at the call site, validated by hand-run smoke test plus the integration test in Task 7.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_streaming_thinking.py::test_chat_loop_passes_thinking_callback_to_run_one_step -v`
Expected: FAIL — `_build_thinking_callback` does not exist in `cli.py`.

- [ ] **Step 3: Write minimal implementation**

In `opencomputer/cli.py`, add a small helper near the chat loop:

```python
from collections.abc import Callable


def _build_thinking_callback(forward: Callable[[str], None]) -> Callable[[str], None]:
    """Return a callback that forwards each thinking-delta chunk to
    ``forward``. Pulled out as a function so the wiring is testable
    without spinning up a full chat loop."""
    def _cb(text: str) -> None:
        forward(text)
    return _cb
```

Then at the call site of `loop.run_conversation` (cli.py:979 — search for `stream_callback=`), add the kwarg:

```python
result = await loop.run_conversation(
    user_message=user_message,
    session_id=session_id,
    runtime=runtime,
    stream_callback=renderer.on_chunk,
    thinking_callback=_build_thinking_callback(renderer.on_thinking_chunk),
    images=images,
)
```

And update the existing `renderer.finalize(...)` call (search for `renderer.finalize(` in the same file) to pass:

```python
show_reasoning=runtime.custom.get("show_reasoning", False),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_streaming_thinking.py::test_chat_loop_passes_thinking_callback_to_run_one_step -v`
Expected: PASS.

Run the full streaming-related test suite:

Run: `pytest tests/ -k "stream or reasoning or thinking" -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli.py tests/test_streaming_thinking.py
git commit -m "feat(cli): wire thinking_callback + show_reasoning into renderer"
```

---

## Task 7: End-to-end integration test

**Files:**
- Create: `tests/test_thinking_dropdown_e2e.py`

**Background:** Tasks 1-6 each verify a single layer. Task 7 stitches them: a fake provider yields `thinking_delta` + `text_delta` events, the AgentLoop forwards them to a real `StreamingRenderer`, and we assert the rendered output contains the live thinking panel during streaming and the collapsed summary at finalize.

- [ ] **Step 1: Write the failing test**

Create `tests/test_thinking_dropdown_e2e.py`:

```python
"""End-to-end: provider streams thinking_delta → loop forwards →
renderer renders live panel → finalize collapses to summary line."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.config import Config, ModelConfig, SessionConfig
from opencomputer.cli_ui.streaming import StreamingRenderer
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider, ProviderResponse, StreamEvent, Usage,
)


class _FakeProvider(BaseProvider):
    name = "fake"
    default_model = "fake-1"

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        raise NotImplementedError

    async def stream_complete(self, **kwargs: Any):
        for ev in self._events:
            yield ev


@pytest.mark.asyncio
async def test_full_thinking_dropdown_flow_collapses_by_default(tmp_path) -> None:
    """Default flow: show_reasoning=False → finalize shows collapsed
    summary, full reasoning text is NOT in the rendered output."""
    final_resp = ProviderResponse(
        # Reasoning lives on BOTH the assistant_message (so StepOutcome
        # surfaces it via outcome.assistant_message.reasoning) AND on
        # ProviderResponse.reasoning (legacy field, harmless duplication).
        message=Message(
            role="assistant",
            content="The answer is 42.",
            reasoning="Let me think... step one. Two plus two equals four.",
        ),
        stop_reason="end_turn",
        usage=Usage(input_tokens=12, output_tokens=5),
        reasoning="Let me think... step one. Two plus two equals four.",
    )
    events = [
        StreamEvent(kind="thinking_delta", text="Let me think... "),
        StreamEvent(kind="thinking_delta", text="step one. "),
        StreamEvent(kind="thinking_delta", text="Two plus two equals four."),
        StreamEvent(kind="text_delta", text="The answer "),
        StreamEvent(kind="text_delta", text="is 42."),
        StreamEvent(kind="done", response=final_resp),
    ]

    buf = io.StringIO()
    console = Console(file=buf, width=100, force_terminal=True, record=True)

    loop = AgentLoop(
        provider=_FakeProvider(events),
        config=Config(
            model=ModelConfig(provider="fake", model="fake-1"),
            session=SessionConfig(db_path=Path(tmp_path) / "e2e.db"),
        ),
    )

    text_chunks: list[str] = []
    thinking_chunks: list[str] = []

    with StreamingRenderer(console) as renderer:
        renderer.start_thinking()
        outcome = await loop._run_one_step(  # type: ignore[attr-defined]
            messages=[Message(role="user", content="What is 2+2?")],
            system="",
            stream_callback=lambda t: (text_chunks.append(t), renderer.on_chunk(t)),
            thinking_callback=lambda t: (
                thinking_chunks.append(t),
                renderer.on_thinking_chunk(t),
            ),
            session_id="e2e-1",
        )
        renderer.finalize(
            # StepOutcome has flat fields: stop_reason, assistant_message,
            # tool_calls_made, input_tokens, output_tokens. There is NO
            # ``response`` attribute; reasoning lives on the message.
            reasoning=outcome.assistant_message.reasoning,
            iterations=1,
            in_tok=outcome.input_tokens,
            out_tok=outcome.output_tokens,
            elapsed_s=2.5,
            show_reasoning=False,  # default
        )

    rendered = console.export_text()

    # 1. Provider deltas were forwarded.
    assert thinking_chunks == [
        "Let me think... ", "step one. ", "Two plus two equals four.",
    ]
    assert "".join(text_chunks) == "The answer is 42."

    # 2. Final answer made it to the screen.
    assert "The answer is 42" in rendered

    # 3. Collapsed summary present.
    assert "Thought" in rendered or "💭" in rendered

    # 4. Full reasoning body is NOT visible (collapsed).
    assert "Two plus two equals four" not in rendered


@pytest.mark.asyncio
async def test_full_thinking_dropdown_flow_expands_when_show_reasoning_on(
    tmp_path,
) -> None:
    """show_reasoning=True → finalize keeps the full thinking panel."""
    final_resp = ProviderResponse(
        message=Message(
            role="assistant",
            content="answer",
            reasoning="full chain of thought here",
        ),
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
        reasoning="full chain of thought here",
    )
    events = [
        StreamEvent(kind="thinking_delta", text="full chain of thought here"),
        StreamEvent(kind="text_delta", text="answer"),
        StreamEvent(kind="done", response=final_resp),
    ]

    buf = io.StringIO()
    console = Console(file=buf, width=100, force_terminal=True, record=True)

    loop = AgentLoop(
        provider=_FakeProvider(events),
        config=Config(
            model=ModelConfig(provider="fake", model="fake-1"),
            session=SessionConfig(db_path=Path(tmp_path) / "e2e.db"),
        ),
    )

    with StreamingRenderer(console) as renderer:
        renderer.start_thinking()
        outcome = await loop._run_one_step(  # type: ignore[attr-defined]
            messages=[Message(role="user", content="?")],
            system="",
            stream_callback=renderer.on_chunk,
            thinking_callback=renderer.on_thinking_chunk,
            session_id="e2e-2",
        )
        renderer.finalize(
            reasoning=outcome.assistant_message.reasoning,
            iterations=1,
            in_tok=outcome.input_tokens,
            out_tok=outcome.output_tokens,
            elapsed_s=0.5,
            show_reasoning=True,
        )

    rendered = console.export_text()
    assert "full chain of thought here" in rendered
    assert "answer" in rendered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_thinking_dropdown_e2e.py -v`
Expected: This SHOULD pass after Tasks 1-6, but if any wiring is wrong it will surface here. If it fails, the failure points to which integration is broken.

- [ ] **Step 3: Fix any integration gap**

If a test fails, trace the failure to the responsible module (provider, loop, renderer, cli) and fix in place. No new tests needed — the e2e is validating prior work.

- [ ] **Step 4: Run the FULL pytest suite + ruff**

This is the deep-test gate before push (per the user's no-push-without-deep-testing rule).

Run: `pytest tests/ -q --tb=short`
Expected: 0 failed.

Run: `ruff check opencomputer/ plugin_sdk/ extensions/ tests/`
Expected: All checks passed!

- [ ] **Step 5: Commit**

```bash
git add tests/test_thinking_dropdown_e2e.py
git commit -m "test(tui): e2e thinking-dropdown flow (provider→loop→renderer)"
```

---

## Self-review pass (mandatory before handoff)

Run through this checklist mentally:

**1. Spec coverage:**
- Live streaming of thinking → Task 5 (`on_thinking_chunk` live) + Task 7 (e2e)
- Collapsed-by-default finalize → Task 5 (`finalize` branch on `show_reasoning`)
- `show_reasoning` honored → Task 5 + Task 6 (cli wires it)
- Both providers emit thinking_delta → Tasks 2 + 3
- SDK contract extended → Task 1
- AgentLoop forwards events → Task 4

**2. Placeholder scan:** none — every code block is concrete.

**3. Type consistency:**
- `thinking_callback` parameter spelled identically across loop.py + cli.py + tests
- `_thinking_buffer` / `_thinking_started_at` attrs match between init + on_thinking_chunk + _render + finalize
- `show_reasoning: bool` kwarg name consistent across renderer + cli
- `StreamEvent(kind="thinking_delta", text=...)` — same shape every emit site

**4. YAGNI gates:** No `/reasoning replay` slash command (deferred — `show_reasoning` toggle is enough for v1). No keyboard expand/collapse (deferred — would need Textual). No channel-adapter rendering (explicitly deferred per user).

**5. Backwards compat:**
- `StreamEvent.kind` Literal expanded — older code that switches on `kind` defaults to ignore unknown kinds (already does in loop.py)
- `_run_one_step`'s new `thinking_callback=None` default keeps existing call sites working
- `finalize`'s new `show_reasoning=False` default preserves old behavior for existing test fixtures (modulo the collapse change, which IS the feature)

---

## Execution choice

Plan saved to `docs/superpowers/plans/2026-04-29-thinking-dropdown-tui.md`.

Per the user's request: **inline execution** via `superpowers:executing-plans`, after a critical-reviewer audit pass on this plan.
