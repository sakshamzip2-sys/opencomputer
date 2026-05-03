# Fix AskUserQuestion + Model-Agnostic Extended Thinking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two independent fixes shipped as TWO separate PRs:
- **Phase A — `AskUserQuestion` no longer hangs.** Today the tool calls `sys.stdin.readline()` which competes with prompt_toolkit + Rich.Live for stdin ownership and never returns ("0.0s running" forever). Replace with a runtime-installed handler that integrates with the active terminal stack.
- **Phase B — Extended thinking works on EVERY provider, not just Anthropic + OpenAI o-series.** Today only providers with native thinking support emit `thinking_delta` events. For non-native providers (gpt-4o, OpenRouter routes, Bedrock proxies, local Llama, etc.), inject a system-prompt instruction telling the model to wrap its reasoning in `<think>...</think>` tags, then transparently parse those tags out of the text stream and emit synthetic `thinking_delta` events to the existing callback chain.

**Architecture:**
- **Phase A:** A `ContextVar`-backed handler in `plugin_sdk/interaction.py`. The CLI installs a Rich-Prompt-based handler at session startup that pauses the active `Rich.Live`, asks the question via `Prompt.ask`, and resumes Live. The tool calls the handler if installed; falls back to today's stdin path only when no handler is present (preserves headless / piped-input behavior).
- **Phase B:** Add `supports_native_thinking: bool = False` to `ProviderCapabilities`. Anthropic and OpenAI providers set it to `True`. At session start, the CLI stashes the active provider's value on `runtime.custom["_provider_supports_native_thinking"]`. A new `ThinkingInjector` (DynamicInjectionProvider) injects a `<think>` instruction whenever effort > "none" AND native is False. A new `ThinkingTagsParser` wraps the provider's stream, intercepts `text_delta` events, and emits `thinking_delta` for the contents of `<think>...</think>` (handling chunk-boundary splits). Wired into the agent loop only when native is False AND effort > "none". The existing `thinking_callback` chain (Reasoning Dropdown v2 work) routes them to the same UI.

**Tech Stack:** Python 3.12+, prompt_toolkit, Rich, pytest, asyncio.

**Out of scope (explicit):**
- AskUserQuestion in async channels (Telegram/Discord) — Phase 11e tracker; existing gateway-mode error message preserved.
- Multi-line freeform input (single-line answer is fine for v1).
- Custom thinking tags beyond `<think>...</think>` (could add `<thinking>` alias as a follow-up).
- Token-counting the injected instruction (small, ~50 tokens; negligible).
- Persisting the parser's "thinking happened" signal back to SessionDB — already covered by the existing `thinking_callback` → `_thinking_buffer` → `ReasoningStore` chain from PR #382.

**Audit-confirmed assumptions (verified before this plan was finalized):**
- `runtime.custom` is a mutable dict on a frozen `RuntimeContext` — assignment mutates the dict, not the field. Verified for the previous Reasoning Dropdown PR (#382), still valid.
- `cli.py:939` constructs ONE `RuntimeContext` per session and reuses it across turns — `runtime.custom` IS shared.
- `InjectionContext` (plugin_sdk/injection.py:23) has `runtime` but does NOT carry the active provider/capabilities. The plan stashes the capability boolean on `runtime.custom["_provider_supports_native_thinking"]` so the injector can read it without an SDK change.
- `ProviderCapabilities` (plugin_sdk/provider_contract.py:103) is `@dataclass(frozen=True, slots=True)`. Adding a new field with a default value is backwards-compatible per the SDK CLAUDE.md rules.
- `BaseProvider.capabilities()` already exists — providers expose this; the loop reads it (line 127-128 area for cache-token decisions). We piggyback on the same chokepoint.
- `StreamEvent.kind` is `Literal["text_delta", "thinking_delta", "tool_call", "done"]` (provider_contract.py:204) — `thinking_delta` is already part of the contract; the parser emits the same kind, no SDK change needed.
- The agent loop already plumbs `thinking_callback` through `_run_one_step` (loop.py:599-600) and conditional dispatches `event.kind == "thinking_delta"` to it — no loop changes for the callback path; only changes for *wrapping* the incoming stream.
- `AskUserQuestionTool` is registered at `cli.py:303`. The CLI is the right install point for the Rich-Prompt handler.
- `Rich.Live(transient=True)` (existing renderer config) supports `live.stop()` mid-stream — output below the panel is preserved on stop and the next `Live(...)` call repaints. This makes pause/resume during the prompt safe.

---

## File Structure

| Path | Action | Phase | Responsibility |
|---|---|---|---|
| `plugin_sdk/interaction.py` | Modify | A | Add `ASK_USER_QUESTION_HANDLER` ContextVar + `AskUserQuestionHandler` Protocol |
| `opencomputer/cli_ui/ask_user_question_handler.py` | **Create** | A | Rich-based handler implementation (pause Live, Prompt.ask, resume) |
| `opencomputer/tools/ask_user_question.py` | Modify | A | Use ContextVar handler when present; fall back to stdin |
| `opencomputer/cli.py` | Modify | A | Install handler at session start (one line) |
| `tests/test_ask_user_question_handler.py` | **Create** | A | Handler unit tests (mock Rich Console) |
| `tests/test_ask_user_question_tool.py` | **Create** | A | Tool tests (handler-installed path + fallback path) |
| `plugin_sdk/provider_contract.py` | Modify | B | Add `supports_native_thinking: bool = False` to `ProviderCapabilities` |
| `extensions/anthropic-provider/provider.py` | Modify | B | Set `supports_native_thinking=True` in capabilities() |
| `extensions/openai-provider/provider.py` | Modify | B | Set `supports_native_thinking=True` in capabilities() |
| `opencomputer/agent/thinking_parser.py` | **Create** | B | `ThinkingTagsParser` async stream wrapper (state machine) |
| `opencomputer/agent/thinking_injector.py` | **Create** | B | `ThinkingInjector(DynamicInjectionProvider)` |
| `opencomputer/cli.py` | Modify | B | Stash `_provider_supports_native_thinking` flag + register injector |
| `opencomputer/agent/loop.py` | Modify | B | Wrap stream with `ThinkingTagsParser` conditionally |
| `tests/test_thinking_parser.py` | **Create** | B | Parser state machine unit tests (incl. chunk splits) |
| `tests/test_thinking_injector.py` | **Create** | B | Injector activation tests |
| `tests/test_thinking_cross_provider.py` | **Create** | B | E2E with mocked non-native provider |

---

# PHASE A: Fix AskUserQuestion hang

Two PRs total — Phase A merges first.

## Task A0: Worktree + branch setup

- [ ] **Step 1:** Create worktree on a fresh branch from `main`

```bash
git -C /Users/saksham/Vscode/claude worktree add \
    /Users/saksham/.config/superpowers/worktrees/claude/ask-user-question-fix \
    -b feat/ask-user-question-fix main
```

- [ ] **Step 2:** cd in + activate venv

```bash
cd /Users/saksham/.config/superpowers/worktrees/claude/ask-user-question-fix/OpenComputer
source /Users/saksham/Vscode/claude/OpenComputer/.venv/bin/activate
```

- [ ] **Step 3:** Baseline test smoke

```bash
pytest tests/ -q 2>&1 | tail -3
```

Expected: green (~7990 passing).

---

## Task A1: Define handler ContextVar + Protocol in plugin_sdk

**Files:**
- Modify: `plugin_sdk/interaction.py`

The handler is async (Rich Prompt is sync but we expose async signature for future Telegram/wire flow):

```python
async def handler(req: InteractionRequest) -> InteractionResponse: ...
```

Storage: a module-level `ContextVar` (async-safe; defaults to `None`).

- [ ] **Step 1:** Append to `plugin_sdk/interaction.py`:

```python
from contextvars import ContextVar
from typing import Awaitable, Callable, Protocol


class AskUserQuestionHandler(Protocol):
    """Async callable that asks the user a question and returns the reply.

    Installed once per session by whichever surface owns the user-input
    layer (today: the CLI's Rich/prompt_toolkit stack; tomorrow: a
    gateway worker that handles the suspend/resume dance).
    """

    def __call__(
        self, req: InteractionRequest
    ) -> Awaitable[InteractionResponse]: ...


#: ContextVar holding the current handler, or ``None`` if no surface has
#: installed one. Tools call ``ASK_USER_QUESTION_HANDLER.get()`` and use
#: the handler if non-None, else fall back to the legacy stdin path.
#:
#: ContextVar (not a module global) so concurrent sessions / subagent
#: contexts each see their own handler — important for delegate trees.
ASK_USER_QUESTION_HANDLER: ContextVar[
    AskUserQuestionHandler | None
] = ContextVar("ASK_USER_QUESTION_HANDLER", default=None)


__all__ = [
    "InteractionRequest",
    "InteractionResponse",
    "AskUserQuestionHandler",
    "ASK_USER_QUESTION_HANDLER",
]
```

- [ ] **Step 2:** Sanity-check that `plugin_sdk/__init__.py` re-exports `InteractionRequest` / `InteractionResponse` (they may already be there). If not, add `AskUserQuestionHandler` and `ASK_USER_QUESTION_HANDLER` to the top-level exports too.

```bash
grep -n "InteractionRequest\|InteractionResponse" plugin_sdk/__init__.py
```

If `InteractionRequest` is already in `__all__`, ALSO add `AskUserQuestionHandler` and `ASK_USER_QUESTION_HANDLER`. If neither is exported, that's fine — leave them as deep imports.

- [ ] **Step 3:** Verify import works

```bash
python -c "from plugin_sdk.interaction import ASK_USER_QUESTION_HANDLER; print(ASK_USER_QUESTION_HANDLER.get())"
```

Expected: `None`.

- [ ] **Step 4:** Commit

```bash
git add plugin_sdk/interaction.py plugin_sdk/__init__.py
git commit -m "feat(sdk): add AskUserQuestionHandler ContextVar protocol"
```

---

## Task A2: Implement Rich-based handler

**Files:**
- Create: `opencomputer/cli_ui/ask_user_question_handler.py`
- Test: `tests/test_ask_user_question_handler.py`

The handler must coexist with the active `Rich.Live`. Strategy:
1. Find the active StreamingRenderer via `current_renderer()` (already exposed for the hook bridge).
2. Stop its `Live` so output to `console.print(...)` from the prompt is not eaten by the live region.
3. Render the question (numbered options if present).
4. Use `Console.input(...)` to read the answer (Rich's input handles terminal mode + history correctly when Live is stopped).
5. Return `InteractionResponse`.
6. The renderer's next chunk-arrival will recreate Live (Live is started lazily in `start_thinking`/on first refresh).

NOTE: We do NOT need to "restart" Live ourselves — the next stream event will trigger `_refresh()` which creates a new Live. This matches how the renderer already handles its own start/stop.

- [ ] **Step 1:** Write the failing tests

```python
# tests/test_ask_user_question_handler.py
"""Unit tests for the Rich-Prompt-based AskUserQuestion handler."""
from __future__ import annotations

import asyncio
import io

import pytest
from rich.console import Console

from opencomputer.cli_ui.ask_user_question_handler import (
    make_rich_handler,
    install_rich_handler,
)
from plugin_sdk.interaction import (
    ASK_USER_QUESTION_HANDLER,
    InteractionRequest,
    InteractionResponse,
)


def _make_console_with_input(text: str) -> tuple[Console, io.StringIO]:
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=80)
    # Monkey-patch input to deliver our scripted answer.
    console.input = lambda prompt="": text  # type: ignore[method-assign]
    return console, out


def test_handler_returns_text_response_for_freeform_question():
    console, _out = _make_console_with_input("forty two")
    handler = make_rich_handler(console=console)
    req = InteractionRequest(question="What's the answer?")
    resp = asyncio.run(handler(req))
    assert isinstance(resp, InteractionResponse)
    assert resp.text == "forty two"
    assert resp.option_index is None


def test_handler_resolves_numeric_option_to_index():
    console, _out = _make_console_with_input("2")
    handler = make_rich_handler(console=console)
    req = InteractionRequest(
        question="Which?", options=("alpha", "beta", "gamma")
    )
    resp = asyncio.run(handler(req))
    assert resp.text == "beta"
    assert resp.option_index == 1


def test_handler_treats_other_choice_as_freeform_passthrough():
    """Last option is the implicit '(other)' — typing its number means
    free-form follows. For v1 we just pass the digit back as text."""
    console, _out = _make_console_with_input("custom answer")
    handler = make_rich_handler(console=console)
    req = InteractionRequest(question="?", options=("a", "b"))
    resp = asyncio.run(handler(req))
    assert resp.text == "custom answer"
    assert resp.option_index is None


def test_handler_renders_question_and_options_to_console():
    console, out = _make_console_with_input("x")
    handler = make_rich_handler(console=console)
    req = InteractionRequest(
        question="Pick one:", options=("alpha", "beta")
    )
    asyncio.run(handler(req))
    text = out.getvalue()
    assert "Pick one:" in text
    assert "alpha" in text
    assert "beta" in text
    # Numbered.
    assert "1" in text and "2" in text


def test_install_rich_handler_sets_contextvar():
    """install_rich_handler installs the handler and returns a token
    suitable for ``reset()``."""
    console, _out = _make_console_with_input("ok")
    assert ASK_USER_QUESTION_HANDLER.get() is None
    token = install_rich_handler(console=console)
    try:
        h = ASK_USER_QUESTION_HANDLER.get()
        assert h is not None
        # Round-trip a call.
        resp = asyncio.run(h(InteractionRequest(question="?")))
        assert resp.text == "ok"
    finally:
        ASK_USER_QUESTION_HANDLER.reset(token)
    assert ASK_USER_QUESTION_HANDLER.get() is None


def test_handler_pauses_active_streaming_renderer():
    """When a StreamingRenderer is active with a running Live, the
    handler must stop it before reading input so the prompt isn't
    swallowed by the live region. After the handler returns, the Live
    state is left stopped — the next stream chunk will recreate it."""
    from rich.console import Console as _Con
    from opencomputer.cli_ui.streaming import StreamingRenderer

    out_console = _Con(file=io.StringIO())
    out_console.input = lambda prompt="": "yes"  # type: ignore[method-assign]
    handler = make_rich_handler(console=out_console)

    renderer = StreamingRenderer(out_console)
    with renderer:
        renderer.start_thinking()
        # Live is now running.
        assert renderer._live is not None
        asyncio.run(handler(InteractionRequest(question="proceed?")))
        # Live should be stopped after the prompt.
        assert renderer._live is None
```

- [ ] **Step 2:** Run tests — expect ImportError

```bash
pytest tests/test_ask_user_question_handler.py -v
```

Expected: `ModuleNotFoundError: opencomputer.cli_ui.ask_user_question_handler`

- [ ] **Step 3:** Create the handler module

```python
# opencomputer/cli_ui/ask_user_question_handler.py
"""Rich-Prompt-based handler for the AskUserQuestion tool.

Installed by the CLI at session start (see ``cli.py``). When the
agent calls ``AskUserQuestionTool`` mid-turn, the tool reaches this
handler via the ``ASK_USER_QUESTION_HANDLER`` ContextVar and uses it
instead of the legacy ``sys.stdin.readline()`` path that conflicts
with the active prompt_toolkit / Rich.Live terminal stack.

Lifecycle:
    1. Discover the active StreamingRenderer (if any) via
       :func:`current_renderer`.
    2. Stop its Live region so the prompt isn't swallowed.
    3. Print the question + numbered options to the console.
    4. Read the answer via ``console.input("> ")``.
    5. Return :class:`InteractionResponse`.

The next stream event from the model will recreate the Live region
naturally (the renderer creates Live lazily in ``start_thinking`` /
``_refresh``), so we don't restart it here.
"""
from __future__ import annotations

from contextvars import Token

from rich.console import Console
from rich.text import Text

from opencomputer.cli_ui.streaming import current_renderer
from plugin_sdk.interaction import (
    ASK_USER_QUESTION_HANDLER,
    AskUserQuestionHandler,
    InteractionRequest,
    InteractionResponse,
)


def _format_question(req: InteractionRequest) -> Text:
    body = Text()
    body.append("\n")
    body.append(req.question, style="bold cyan")
    body.append("\n")
    if req.options:
        for i, opt in enumerate(req.options, 1):
            body.append(f"  {i}. ", style="dim")
            body.append(opt)
            body.append("\n")
        body.append(
            f"  {len(req.options) + 1}. (other — type free-form text)\n",
            style="dim italic",
        )
    return body


def _resolve_option(
    req: InteractionRequest, raw: str
) -> InteractionResponse:
    """Map a numeric pick to its option text + index; otherwise
    treat as free-form."""
    answer = raw.strip()
    if req.options and answer.isdigit():
        idx = int(answer) - 1
        if 0 <= idx < len(req.options):
            return InteractionResponse(
                text=req.options[idx], option_index=idx
            )
    return InteractionResponse(text=answer, option_index=None)


def make_rich_handler(*, console: Console) -> AskUserQuestionHandler:
    """Build a handler that reads from the given Rich Console."""

    async def _handler(req: InteractionRequest) -> InteractionResponse:
        # Pause the active Live region so the prompt isn't eaten.
        active = current_renderer()
        if active is not None and active._live is not None:
            try:
                active._live.stop()
            except Exception:  # noqa: BLE001 — never crash the tool
                pass
            active._live = None  # let the next stream tick recreate it
        console.print(_format_question(req))
        try:
            raw = console.input("> ")
        except (EOFError, KeyboardInterrupt) as exc:
            raise exc  # tool's execute() converts to is_error=True
        return _resolve_option(req, raw)

    return _handler


def install_rich_handler(*, console: Console) -> Token:
    """Install the handler in the current async context and return the
    ContextVar reset token. The CLI calls this once at session start
    and lets the token live until process exit (no reset)."""
    handler = make_rich_handler(console=console)
    return ASK_USER_QUESTION_HANDLER.set(handler)


__all__ = ["make_rich_handler", "install_rich_handler"]
```

- [ ] **Step 4:** Run tests

```bash
pytest tests/test_ask_user_question_handler.py -v
```

Expected: 6 passed.

- [ ] **Step 5:** Commit

```bash
git add opencomputer/cli_ui/ask_user_question_handler.py tests/test_ask_user_question_handler.py
git commit -m "feat(cli_ui): Rich-Prompt handler for AskUserQuestion (no more stdin conflict)"
```

---

## Task A3: Tool uses handler when present

**Files:**
- Modify: `opencomputer/tools/ask_user_question.py`
- Test: `tests/test_ask_user_question_tool.py`

- [ ] **Step 1:** Write the failing tests

```python
# tests/test_ask_user_question_tool.py
"""Tool-level tests: handler-installed path + stdin fallback."""
from __future__ import annotations

import asyncio

import pytest

from opencomputer.tools.ask_user_question import AskUserQuestionTool
from plugin_sdk.core import ToolCall
from plugin_sdk.interaction import (
    ASK_USER_QUESTION_HANDLER,
    InteractionRequest,
    InteractionResponse,
)


def _call(args: dict) -> ToolCall:
    return ToolCall(id="t1", name="AskUserQuestion", arguments=args)


def test_tool_uses_installed_handler_when_present():
    captured: list[InteractionRequest] = []

    async def fake_handler(req: InteractionRequest) -> InteractionResponse:
        captured.append(req)
        return InteractionResponse(text="forty two", option_index=None)

    token = ASK_USER_QUESTION_HANDLER.set(fake_handler)
    try:
        tool = AskUserQuestionTool(cli_mode=True)
        result = asyncio.run(
            tool.execute(_call({"question": "What's the answer?"}))
        )
    finally:
        ASK_USER_QUESTION_HANDLER.reset(token)

    assert not result.is_error
    assert "forty two" in result.content
    assert len(captured) == 1
    assert captured[0].question == "What's the answer?"


def test_tool_passes_options_through_to_handler():
    captured: list[InteractionRequest] = []

    async def fake_handler(req):
        captured.append(req)
        return InteractionResponse(text="alpha", option_index=0)

    token = ASK_USER_QUESTION_HANDLER.set(fake_handler)
    try:
        tool = AskUserQuestionTool(cli_mode=True)
        result = asyncio.run(
            tool.execute(_call({"question": "?", "options": ["alpha", "beta"]}))
        )
    finally:
        ASK_USER_QUESTION_HANDLER.reset(token)

    assert not result.is_error
    assert "alpha" in result.content
    assert captured[0].options == ("alpha", "beta")


def test_tool_falls_back_to_stdin_when_no_handler_installed(monkeypatch):
    """When no handler is installed, the legacy stdin path is used —
    preserves headless / piped input behavior."""
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("piped answer\n"))
    # Belt-and-suspenders: ensure the contextvar is empty.
    if ASK_USER_QUESTION_HANDLER.get() is not None:
        token = ASK_USER_QUESTION_HANDLER.set(None)
    else:
        token = None

    try:
        tool = AskUserQuestionTool(cli_mode=True)
        result = asyncio.run(tool.execute(_call({"question": "?"})))
    finally:
        if token is not None:
            ASK_USER_QUESTION_HANDLER.reset(token)

    assert not result.is_error
    assert "piped answer" in result.content


def test_tool_handler_keyboard_interrupt_returns_cancelled():
    async def cancel_handler(req):
        raise KeyboardInterrupt()

    token = ASK_USER_QUESTION_HANDLER.set(cancel_handler)
    try:
        tool = AskUserQuestionTool(cli_mode=True)
        result = asyncio.run(tool.execute(_call({"question": "?"})))
    finally:
        ASK_USER_QUESTION_HANDLER.reset(token)

    assert result.is_error
    assert "cancel" in result.content.lower()


def test_tool_gateway_mode_unchanged_when_no_handler():
    """Async-channel mode is detected by cli_mode=False — must still
    return the existing 'use PushNotification' error message."""
    tool = AskUserQuestionTool(cli_mode=False)
    result = asyncio.run(tool.execute(_call({"question": "?"})))
    assert result.is_error
    assert "PushNotification" in result.content
```

- [ ] **Step 2:** Run tests — expect failures (current tool ignores the ContextVar)

```bash
pytest tests/test_ask_user_question_tool.py -v
```

Expected: 4 of 5 fail (the gateway-mode one passes).

- [ ] **Step 3:** Modify the tool to check the ContextVar first

Edit `opencomputer/tools/ask_user_question.py`:

a) Add import at top:

```python
from plugin_sdk.interaction import (
    ASK_USER_QUESTION_HANDLER,
    InteractionRequest,
    InteractionResponse,
)
```

(`InteractionRequest` is already imported — keep that import; add the others.)

b) Replace the `execute` method's prompt section (the `req = InteractionRequest(...)` ... `try: answer = _prompt_stdin(req)` block, lines 113-126 of current file) with:

```python
        req = InteractionRequest(
            question=question,
            options=options,
            presentation="choice" if options else "text",
        )

        # Prefer the installed handler (CLI's Rich-Prompt path). Falls
        # back to legacy stdin only when no surface has installed one
        # (headless scripts, piped input, tests without handler).
        handler = ASK_USER_QUESTION_HANDLER.get()
        try:
            if handler is not None:
                resp = await handler(req)
                if resp.option_index is not None:
                    return ToolResult(
                        tool_call_id=call.id,
                        content=(
                            f"User chose option {resp.option_index + 1}: "
                            f"{options[resp.option_index]}"
                        ),
                    )
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"User answered: {resp.text}",
                )
            # Legacy fallback path.
            answer = _prompt_stdin(req)
        except (EOFError, KeyboardInterrupt):
            return ToolResult(
                tool_call_id=call.id,
                content="Error: user cancelled (EOF or Ctrl-C)",
                is_error=True,
            )

        # Legacy stdin path: numeric option expansion.
        if options and answer.strip().isdigit():
            idx = int(answer.strip()) - 1
            if 0 <= idx < len(options):
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"User chose option {idx + 1}: {options[idx]}",
                )
        return ToolResult(
            tool_call_id=call.id,
            content=f"User answered: {answer}",
        )
```

- [ ] **Step 4:** Run tests

```bash
pytest tests/test_ask_user_question_tool.py -v
```

Expected: 5 passed.

- [ ] **Step 5:** Commit

```bash
git add opencomputer/tools/ask_user_question.py tests/test_ask_user_question_tool.py
git commit -m "fix(tools): AskUserQuestion uses installed handler instead of raw stdin"
```

---

## Task A4: Wire handler installation in CLI

**Files:**
- Modify: `opencomputer/cli.py`

- [ ] **Step 1:** Locate the renderer creation site

```bash
grep -n "StreamingRenderer\|use_live_ui\|console = " opencomputer/cli.py | head -10
```

You'll see `console` is constructed somewhere upstream. The handler should be installed once per session, near where `runtime` is constructed (we already added the reasoning_store there for PR #382).

- [ ] **Step 2:** Add handler installation right after the `_reasoning_store` install (which is right after `runtime = RuntimeContext(...)`):

```python
    # Install the AskUserQuestion handler so the tool integrates with
    # our Rich/prompt_toolkit terminal stack instead of competing with
    # it via raw stdin reads.
    from opencomputer.cli_ui.ask_user_question_handler import (
        install_rich_handler as _install_auq,
    )
    if use_live_ui:
        _install_auq(console=console)
```

The `if use_live_ui:` guard ensures headless / non-TTY runs keep the legacy stdin path (which works for piped input in scripts).

- [ ] **Step 3:** Smoke import

```bash
python -c "from opencomputer.cli_ui.ask_user_question_handler import install_rich_handler; print('OK')"
```

Expected: `OK`.

- [ ] **Step 4:** Manual smoke test (since this is an interactive feature)

```bash
opencomputer
> can you ask me a question via AskUserQuestion?
# Watch: when AskUserQuestion fires, the prompt should appear
# IMMEDIATELY (not "0.0s running" forever), accept input, and the
# answer round-trips back to the model.
```

- [ ] **Step 5:** Commit

```bash
git add opencomputer/cli.py
git commit -m "feat(cli): install AskUserQuestion Rich handler at session start"
```

---

## Task A5: Phase A push + PR

- [ ] **Step 1:** Final test run

```bash
pytest tests/ -q 2>&1 | tail -3
```

Expected: green, ~7990+11 = 8001+ tests passing.

- [ ] **Step 2:** Lint

```bash
ruff check opencomputer/ plugin_sdk/ tests/ 2>&1 | tail -5
```

If any auto-fixable issues, run `ruff check --fix`.

- [ ] **Step 3:** Push + PR

```bash
git push -u origin feat/ask-user-question-fix
gh pr create --title "fix: AskUserQuestion no longer hangs forever (Rich handler)" \
  --body "$(cat <<'EOF'
## Summary
- AskUserQuestion now actually works in the chat REPL. Previously it called `sys.stdin.readline()` which competes with prompt_toolkit + Rich.Live for stdin ownership and never returned ("0.0s running" forever).
- Adds `ASK_USER_QUESTION_HANDLER` ContextVar in `plugin_sdk/interaction.py`. The CLI installs a Rich-Prompt-based handler at session start that pauses the active Live region, asks the question, reads the answer, and lets the next stream tick recreate Live.
- Backwards compat: when no handler is installed (headless / piped-input mode), the tool falls back to the existing stdin path. Gateway-mode error message preserved.

## Test plan
- [x] `tests/test_ask_user_question_handler.py` — 6 tests for the handler itself (free-form, numeric options, console rendering, ContextVar install, Live pause)
- [x] `tests/test_ask_user_question_tool.py` — 5 tests for tool behavior (handler path, options, stdin fallback, Ctrl+C, gateway-mode unchanged)
- [x] Manual smoke: ran `opencomputer`, asked the agent to invoke AskUserQuestion, confirmed prompt appears + answer round-trips

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

# PHASE B: Cross-Provider Extended Thinking

After Phase A merges, start Phase B in a NEW worktree from the latest `main` (so it picks up Phase A's changes if they affect anything; in practice they don't overlap).

## Task B0: Worktree + branch setup

- [ ] **Step 1:** Update local `main` (after Phase A PR merges)

```bash
git -C /Users/saksham/Vscode/claude fetch origin main
git -C /Users/saksham/Vscode/claude checkout main
git -C /Users/saksham/Vscode/claude pull origin main
```

- [ ] **Step 2:** Create new worktree

```bash
git -C /Users/saksham/Vscode/claude worktree add \
    /Users/saksham/.config/superpowers/worktrees/claude/model-agnostic-thinking \
    -b feat/model-agnostic-thinking main
```

- [ ] **Step 3:** cd + venv + baseline

```bash
cd /Users/saksham/.config/superpowers/worktrees/claude/model-agnostic-thinking/OpenComputer
source /Users/saksham/Vscode/claude/OpenComputer/.venv/bin/activate
pytest tests/ -q 2>&1 | tail -3
```

Expected: green.

---

## Task B1: Add `supports_native_thinking` to ProviderCapabilities

**Files:**
- Modify: `plugin_sdk/provider_contract.py`
- Test: `tests/test_provider_capabilities_supports_native_thinking.py` (create)

- [ ] **Step 1:** Write the failing test

```python
# tests/test_provider_capabilities_supports_native_thinking.py
"""ProviderCapabilities exposes a backwards-compatible
supports_native_thinking flag (default False)."""
from __future__ import annotations

from plugin_sdk.provider_contract import ProviderCapabilities


def test_default_supports_native_thinking_is_false():
    """Conservative default — providers must opt in."""
    caps = ProviderCapabilities()
    assert caps.supports_native_thinking is False


def test_supports_native_thinking_can_be_set_true():
    caps = ProviderCapabilities(supports_native_thinking=True)
    assert caps.supports_native_thinking is True


def test_existing_capabilities_unaffected():
    """Backwards compat: existing fields keep their defaults when
    supports_native_thinking is the only argument."""
    caps = ProviderCapabilities(supports_native_thinking=True)
    assert caps.requires_reasoning_resend_in_tool_cycle is False
    assert caps.reasoning_block_kind is None
```

- [ ] **Step 2:** Run — expect failure

```bash
pytest tests/test_provider_capabilities_supports_native_thinking.py -v
```

Expected: TypeError about unknown field.

- [ ] **Step 3:** Add the field to `plugin_sdk/provider_contract.py`

In the `ProviderCapabilities` dataclass (around line 126-132), add:

```python
    supports_native_thinking: bool = False
    """True if the provider has a native extended-thinking / reasoning API
    that already emits ``thinking_delta`` events from
    :meth:`stream_complete`. When False, the agent loop activates the
    prompt-based fallback (system-prompt instruction + ``<think>...</think>``
    tag parser) so users get model-agnostic thinking visibility.

    Default ``False`` — existing providers see no behaviour change until
    they opt in."""
```

Update the docstring of the dataclass to mention the new field.

- [ ] **Step 4:** Run tests

```bash
pytest tests/test_provider_capabilities_supports_native_thinking.py -v
```

Expected: 3 passed.

- [ ] **Step 5:** Commit

```bash
git add plugin_sdk/provider_contract.py tests/test_provider_capabilities_supports_native_thinking.py
git commit -m "feat(sdk): add ProviderCapabilities.supports_native_thinking flag"
```

---

## Task B2: Set capability True on Anthropic + OpenAI providers

**Files:**
- Modify: `extensions/anthropic-provider/provider.py`
- Modify: `extensions/openai-provider/provider.py`
- Test: `tests/test_anthropic_capabilities.py` (likely exists — extend) and `tests/test_openai_capabilities.py` (likely exists — extend), or create if absent.

For Anthropic: extended thinking is always available on Sonnet/Opus 4+ models. Set `supports_native_thinking=True` unconditionally.

For OpenAI: only o1/o3/o4 series have native reasoning. The capability is therefore model-dependent. Strategy: report `True` if and only if the configured model is in the o-series. This keeps gpt-4o etc. routed through the prompt-based fallback so users get thinking on those models too.

- [ ] **Step 1:** Find the existing capabilities() methods

```bash
grep -n "def capabilities\|ProviderCapabilities(" extensions/anthropic-provider/provider.py extensions/openai-provider/provider.py
```

- [ ] **Step 2:** Anthropic — model-dependent

Anthropic has older models (Opus 3.5 etc.) that lack native extended thinking. Setting `supports_native_thinking=True` unconditionally would silently disable the fallback for those users — they'd see NO thinking. Use the existing `supports_adaptive_thinking(model)` helper (already imported at provider.py:31) to gate the capability.

In `extensions/anthropic-provider/provider.py`, locate the existing `ProviderCapabilities(...)` construction and add the new kwarg:

```python
        return ProviderCapabilities(
            requires_reasoning_resend_in_tool_cycle=True,
            reasoning_block_kind="anthropic_thinking",
            extracts_cache_tokens=_extract_cache_tokens_anthropic,
            min_cache_tokens=_min_cache_tokens_anthropic,
            supports_long_ttl=True,
            supports_native_thinking=supports_adaptive_thinking(self.model),
        )
```

(Mirror the existing kwargs — only ADD the new one.)

If the Anthropic provider has multiple `capabilities()` paths (one per model class), update each consistently. Search for `ProviderCapabilities(` across the file and confirm all call sites get the new kwarg.

- [ ] **Step 3:** OpenAI — model-dependent

In `extensions/openai-provider/provider.py`, the `capabilities()` method needs access to the model name. If it's a method on the provider instance, `self.model` should be available. Add a helper at module top:

```python
_REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")  # update as new
                                                          # series ship


def _is_native_reasoning_model(model: str) -> bool:
    """OpenAI o-series + future native-reasoning models. Used to set
    ProviderCapabilities.supports_native_thinking — when False, the
    agent loop activates the prompt-based <think>-tag fallback so even
    gpt-4o sees model-agnostic thinking."""
    name = (model or "").lower()
    return any(name.startswith(p) or name.startswith(f"openai/{p}")
               for p in _REASONING_MODEL_PREFIXES)
```

In `capabilities()`:

```python
        return ProviderCapabilities(
            ...existing kwargs...,
            supports_native_thinking=_is_native_reasoning_model(self.model),
        )
```

- [ ] **Step 4:** Add tests

If `tests/test_anthropic_capabilities.py` exists, extend it; else create a small file:

```python
# tests/test_anthropic_provider_native_thinking.py
import pytest


@pytest.mark.parametrize("model,expected", [
    ("claude-sonnet-4-7", True),       # modern → native
    ("claude-opus-4-7", True),         # modern → native
    ("claude-3-5-sonnet-20241022", False),  # legacy → no native
    ("claude-3-opus-20240229", False),      # legacy → no native
])
def test_anthropic_capabilities_supports_native_thinking_only_for_modern_models(
    model, expected
):
    """Models without native thinking must report False so the
    fallback (prompt + parser) kicks in for those users too."""
    from extensions.anthropic_provider.provider import (
        AnthropicProvider,  # adjust path/name to actual import shape
    )
    p = AnthropicProvider(api_key="sk-fake", model=model)
    assert p.capabilities().supports_native_thinking is expected
```

If `supports_adaptive_thinking()` exposes a different model-list semantic than expected, adjust the parametrize cases to match what it actually returns. Run `python -c "from extensions.anthropic_provider.provider import supports_adaptive_thinking; print(supports_adaptive_thinking('claude-3-5-sonnet-20241022'))"` to verify before writing tests.

```python
# tests/test_openai_provider_native_thinking.py
import pytest


@pytest.mark.parametrize("model,expected", [
    ("o1-preview", True),
    ("o3-mini", True),
    ("o4", True),
    ("gpt-5", True),
    ("gpt-4o", False),
    ("gpt-4-turbo", False),
    ("gpt-3.5-turbo", False),
    ("openai/o1", True),
    ("openai/gpt-4o", False),
])
def test_openai_capabilities_supports_native_thinking_only_for_reasoning_models(
    model, expected
):
    from extensions.openai_provider.provider import OpenAIProvider  # adjust
    p = OpenAIProvider(api_key="sk-fake", model=model)
    assert p.capabilities().supports_native_thinking is expected
```

NOTE on imports: providers in `extensions/` typically need a special importlib path because of the loader's synthetic module names. Check existing tests (`test_anthropic_*.py`) for the import incantation and mirror it.

- [ ] **Step 5:** Run

```bash
pytest tests/test_anthropic_provider_native_thinking.py tests/test_openai_provider_native_thinking.py -v
```

Expected: all pass.

- [ ] **Step 6:** Commit

```bash
git add extensions/anthropic-provider/provider.py extensions/openai-provider/provider.py tests/test_anthropic_provider_native_thinking.py tests/test_openai_provider_native_thinking.py
git commit -m "feat(providers): Anthropic + OpenAI o-series declare native thinking support"
```

---

## Task B3: ThinkingTagsParser — async stream wrapper state machine

**Files:**
- Create: `opencomputer/agent/thinking_parser.py`
- Test: `tests/test_thinking_parser.py`

The parser wraps `provider.stream_complete()`'s `AsyncIterator[StreamEvent]`. For each `text_delta` event, it walks the text looking for `<think>` open and `</think>` close, splitting at tag boundaries and emitting `StreamEvent(kind="thinking_delta", text=...)` for the inside-tag content, while suppressing the tags themselves and emitting normal `text_delta` for the outside content.

State machine:
- `_in_thinking: bool` — currently inside a `<think>...</think>` block
- `_partial_buffer: str` — last chars not yet matched (in case `<think>` is split across delta boundaries)

Algorithm per delta:
1. Concatenate `_partial_buffer + delta.text`.
2. Loop:
   - If `_in_thinking`: search for `</think>`.
     - If found: emit `thinking_delta(text=pre)`, advance past tag, set `_in_thinking=False`, continue.
     - If not found: emit `thinking_delta(text=text_minus_tail_buffer)`, hold the last 8 chars (= len(`</think>`)) in `_partial_buffer`, break.
   - If NOT `_in_thinking`: search for `<think>`.
     - If found: emit `text_delta(text=pre)`, advance past tag, set `_in_thinking=True`, continue.
     - If not found: emit `text_delta(text=text_minus_tail_buffer)`, hold the last 7 chars (= len(`<think>`)) in `_partial_buffer`, break.
3. On stream `done`: flush buffer (if `_in_thinking`, emit remaining as thinking; else as text).

- [ ] **Step 1:** Write the failing tests

```python
# tests/test_thinking_parser.py
"""ThinkingTagsParser: async stream wrapper that extracts <think>...</think>
content out of text_delta events and emits thinking_delta events for the
contents."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from opencomputer.agent.thinking_parser import ThinkingTagsParser
from plugin_sdk.provider_contract import StreamEvent


async def _to_list(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [e async for e in it]


async def _from_chunks(*chunks: str) -> AsyncIterator[StreamEvent]:
    for c in chunks:
        yield StreamEvent(kind="text_delta", text=c)
    yield StreamEvent(kind="done")


def _kinds(events) -> list[str]:
    return [e.kind for e in events]


def _texts(events, kind) -> str:
    return "".join(e.text for e in events if e.kind == kind)


def test_passthrough_when_no_think_tags():
    """Pure text — every chunk passes through unchanged."""
    parser = ThinkingTagsParser()
    src = _from_chunks("hello ", "world")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _kinds(out) == ["text_delta", "text_delta", "done"]
    assert _texts(out, "text_delta") == "hello world"


def test_extracts_single_think_block():
    parser = ThinkingTagsParser()
    src = _from_chunks("answer is <think>let me reason</think> 42")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "thinking_delta") == "let me reason"
    assert _texts(out, "text_delta") == "answer is  42"


def test_handles_open_tag_split_across_chunks():
    """The <think> tag is split mid-tag at the chunk boundary —
    parser must stitch it together via the partial buffer."""
    parser = ThinkingTagsParser()
    src = _from_chunks("hi <th", "ink>secret</think> bye")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "thinking_delta") == "secret"
    assert _texts(out, "text_delta") == "hi  bye"


def test_handles_close_tag_split_across_chunks():
    parser = ThinkingTagsParser()
    src = _from_chunks("hi <think>secret</thi", "nk> bye")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "thinking_delta") == "secret"
    assert _texts(out, "text_delta") == "hi  bye"


def test_handles_chunk_starting_inside_thinking():
    parser = ThinkingTagsParser()
    src = _from_chunks("<think>line1\n", "line2</think>done")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "thinking_delta") == "line1\nline2"
    assert _texts(out, "text_delta") == "done"


def test_multiple_think_blocks_in_one_response():
    parser = ThinkingTagsParser()
    src = _from_chunks("<think>a</think>x<think>b</think>y")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "thinking_delta") == "ab"
    assert _texts(out, "text_delta") == "xy"


def test_unclosed_think_tag_flushes_remaining_as_thinking_on_done():
    """If the model emits <think> but never </think>, on stream end we
    flush the remaining buffer as thinking. Defensive — better than
    losing the content silently."""
    parser = ThinkingTagsParser()
    src = _from_chunks("hi <think>never closes")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "text_delta") == "hi "
    assert _texts(out, "thinking_delta") == "never closes"
    # done event present at end.
    assert out[-1].kind == "done"


def test_passes_non_text_events_through_untouched():
    """tool_call, done, and other event kinds must NOT be inspected by
    the parser — they pass through verbatim."""
    parser = ThinkingTagsParser()

    async def src():
        yield StreamEvent(kind="text_delta", text="<think>x</think>y")
        yield StreamEvent(kind="tool_call", tool_call=None)  # type: ignore
        yield StreamEvent(kind="done")

    out = asyncio.run(_to_list(parser.wrap(src())))
    assert any(e.kind == "tool_call" for e in out)
    assert _texts(out, "thinking_delta") == "x"
    assert _texts(out, "text_delta") == "y"


def test_empty_think_block_is_dropped_cleanly():
    parser = ThinkingTagsParser()
    src = _from_chunks("a<think></think>b")
    out = asyncio.run(_to_list(parser.wrap(src)))
    # No thinking_delta event should be emitted for an empty block.
    assert all(e.kind != "thinking_delta" or e.text != "" for e in out)
    assert _texts(out, "text_delta") == "ab"


def test_buffer_flushes_pending_text_on_done():
    """If the stream ends with un-flushed buffer (e.g. ends mid '<th'
    that turned out NOT to be a tag), the remaining bytes flush as
    text on done."""
    parser = ThinkingTagsParser()
    src = _from_chunks("hello <th")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "text_delta") == "hello <th"
```

- [ ] **Step 2:** Run — expect ImportError

```bash
pytest tests/test_thinking_parser.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3:** Implement the parser

```python
# opencomputer/agent/thinking_parser.py
"""Async stream wrapper that extracts ``<think>...</think>`` blocks out
of text-delta events.

Used by the agent loop when the active provider does NOT have native
extended-thinking support (e.g. gpt-4o, OpenRouter routes to non-
thinking models, local Llama). A complementary
``ThinkingInjector`` adds a system-prompt instruction telling the
model to use these tags; this parser then transparently routes the
contents to the existing ``thinking_callback`` chain so the
StreamingRenderer / ReasoningStore pipeline picks them up unchanged.

State machine (per stream):
    - ``_in_thinking: bool`` — whether the next text bytes belong
      inside a thinking block.
    - ``_partial: str`` — bytes held back from emission because they
      MIGHT be the start of a tag whose closure hasn't arrived yet.

Tag-boundary safety: tags can split arbitrarily across chunk boundaries
(``<th`` then ``ink>``). We hold back at most ``len("</think>")`` chars
between iterations, then on the next chunk concatenate and re-scan.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from plugin_sdk.provider_contract import StreamEvent


_OPEN_TAG = "<think>"
_CLOSE_TAG = "</think>"
_LONGEST_TAG = max(len(_OPEN_TAG), len(_CLOSE_TAG))


class ThinkingTagsParser:
    """Wraps an ``AsyncIterator[StreamEvent]`` and extracts thinking
    tags from ``text_delta`` events."""

    def __init__(self) -> None:
        self._in_thinking = False
        self._partial = ""

    async def wrap(
        self, source: AsyncIterator[StreamEvent]
    ) -> AsyncIterator[StreamEvent]:
        async for event in source:
            if event.kind != "text_delta":
                # Non-text events pass through unchanged. If the stream
                # ends, flush any held-back partial bytes first.
                if event.kind == "done":
                    async for flush in self._flush():
                        yield flush
                yield event
                continue

            text = self._partial + (event.text or "")
            self._partial = ""

            # Walk through the text emitting events in order. Loop
            # because one chunk can contain multiple tag transitions.
            while text:
                if self._in_thinking:
                    close_at = text.find(_CLOSE_TAG)
                    if close_at == -1:
                        # No close tag in this chunk. Emit everything
                        # except the trailing partial-tag suffix.
                        emit, hold = _split_with_tag_buffer(text, _CLOSE_TAG)
                        if emit:
                            yield StreamEvent(
                                kind="thinking_delta", text=emit
                            )
                        self._partial = hold
                        text = ""
                    else:
                        # Found close tag.
                        if close_at > 0:
                            yield StreamEvent(
                                kind="thinking_delta",
                                text=text[:close_at],
                            )
                        text = text[close_at + len(_CLOSE_TAG):]
                        self._in_thinking = False
                else:
                    open_at = text.find(_OPEN_TAG)
                    if open_at == -1:
                        # No open tag in this chunk. Emit everything
                        # except the trailing partial-tag suffix.
                        emit, hold = _split_with_tag_buffer(text, _OPEN_TAG)
                        if emit:
                            yield StreamEvent(
                                kind="text_delta", text=emit
                            )
                        self._partial = hold
                        text = ""
                    else:
                        # Found open tag.
                        if open_at > 0:
                            yield StreamEvent(
                                kind="text_delta", text=text[:open_at],
                            )
                        text = text[open_at + len(_OPEN_TAG):]
                        self._in_thinking = True

    async def _flush(self) -> AsyncIterator[StreamEvent]:
        """Emit any held-back partial buffer at stream end."""
        if not self._partial:
            return
        if self._in_thinking:
            yield StreamEvent(kind="thinking_delta", text=self._partial)
        else:
            yield StreamEvent(kind="text_delta", text=self._partial)
        self._partial = ""


def _split_with_tag_buffer(text: str, tag: str) -> tuple[str, str]:
    """Split ``text`` so the trailing portion that COULD be the start
    of ``tag`` is held back for the next chunk.

    Example: tag=``<think>``, text=``hello <th`` → emit=``hello ``,
    hold=``<th``. Next chunk ``ink>`` will be concatenated and the full
    tag detected.

    Conservative: only holds back if the tail genuinely matches a
    non-empty prefix of the tag. Avoids stalling on text like
    ``hello !`` where no part of ``!`` could ever be a tag start.
    """
    n = len(tag)
    # Find the longest tag-prefix that is a suffix of text.
    for k in range(min(n - 1, len(text)), 0, -1):
        if text.endswith(tag[:k]):
            return text[:-k], text[-k:]
    return text, ""


__all__ = ["ThinkingTagsParser"]
```

- [ ] **Step 4:** Run tests

```bash
pytest tests/test_thinking_parser.py -v
```

Expected: 10 passed.

- [ ] **Step 5:** Commit

```bash
git add opencomputer/agent/thinking_parser.py tests/test_thinking_parser.py
git commit -m "feat(agent): ThinkingTagsParser extracts <think>...</think> from text streams"
```

---

## Task B4: ThinkingInjector — DynamicInjectionProvider

**Files:**
- Create: `opencomputer/agent/thinking_injector.py`
- Test: `tests/test_thinking_injector.py`

The injector adds a system-prompt instruction telling the model to use `<think>...</think>` tags. Activates when:
1. `runtime.custom["reasoning_effort"]` is something other than `"none"` (default `"medium"` per existing reasoning_cmd.py)
2. `runtime.custom["_provider_supports_native_thinking"]` is False (we wire this in Task B5)

If the active provider DOES support native thinking, the injector is a no-op — native API path handles it.

- [ ] **Step 1:** Write the failing tests

```python
# tests/test_thinking_injector.py
"""ThinkingInjector activates a <think>-tag instruction in the system
prompt only when (a) reasoning effort is set, and (b) the active
provider lacks native thinking support."""
from __future__ import annotations

import asyncio

import pytest

from opencomputer.agent.thinking_injector import (
    ThinkingInjector,
    _INSTRUCTION,
)
from plugin_sdk.injection import InjectionContext
from plugin_sdk.runtime_context import RuntimeContext


def _ctx(*, effort=None, native=None) -> InjectionContext:
    rt = RuntimeContext()
    if effort is not None:
        rt.custom["reasoning_effort"] = effort
    if native is not None:
        rt.custom["_provider_supports_native_thinking"] = native
    return InjectionContext(messages=(), runtime=rt)


def test_provider_id_is_stable():
    injector = ThinkingInjector()
    assert injector.provider_id == "thinking_tags_fallback"


def test_priority_runs_after_plan_yolo_modes():
    """plan=10, yolo=20; thinking should land later so primary modes win."""
    injector = ThinkingInjector()
    assert injector.priority >= 50


def test_returns_none_when_native_thinking_supported():
    injector = ThinkingInjector()
    out = asyncio.run(injector.collect(_ctx(effort="high", native=True)))
    assert out is None


def test_returns_none_when_effort_is_none():
    injector = ThinkingInjector()
    out = asyncio.run(injector.collect(_ctx(effort="none", native=False)))
    assert out is None


def test_returns_none_when_effort_unset_and_native_false():
    """Effort defaults to 'medium' per reasoning_cmd._DEFAULT_LEVEL —
    when not explicitly set, the injector should still kick in for
    non-native providers because the default IS effective use."""
    injector = ThinkingInjector()
    out = asyncio.run(injector.collect(_ctx(effort=None, native=False)))
    # Default effort 'medium' means: yes, inject.
    assert out is not None
    assert "<think>" in out


def test_returns_instruction_when_native_false_and_effort_set():
    injector = ThinkingInjector()
    out = asyncio.run(injector.collect(_ctx(effort="high", native=False)))
    assert out is not None
    assert "<think>" in out and "</think>" in out
    # Mentions the contract clearly.
    assert "reasoning" in out.lower()


def test_default_provider_assumption_is_no_native_support():
    """If the runtime hasn't been told about provider capabilities
    (e.g. wire path before Task B5 wires it), default to assuming NO
    native support — model-agnostic fallback is the safer default."""
    injector = ThinkingInjector()
    rt = RuntimeContext()
    rt.custom["reasoning_effort"] = "medium"
    # Note: NOT setting _provider_supports_native_thinking.
    out = asyncio.run(injector.collect(InjectionContext(messages=(), runtime=rt)))
    assert out is not None
```

- [ ] **Step 2:** Run — expect ImportError

```bash
pytest tests/test_thinking_injector.py -v
```

- [ ] **Step 3:** Implement the injector

```python
# opencomputer/agent/thinking_injector.py
"""DynamicInjectionProvider that adds a ``<think>...</think>`` system
instruction when the active provider lacks native extended thinking.

Pairs with :class:`opencomputer.agent.thinking_parser.ThinkingTagsParser`,
which extracts the contents of those tags from the text stream and
routes them to the existing ``thinking_callback`` chain so the
StreamingRenderer + ReasoningStore (PR #382) pick them up unchanged.

Activates when:
    1. ``runtime.custom["reasoning_effort"]`` is anything other than
       ``"none"`` (default is ``"medium"`` per the reasoning_cmd
       contract — so unset === active).
    2. ``runtime.custom["_provider_supports_native_thinking"]`` is
       falsy. CLI wires this at session start by reading the active
       provider's :class:`ProviderCapabilities`. If the wire isn't
       set (e.g. wire-protocol path), default to ``False`` so the
       fallback kicks in — safer than silently dropping thinking.
"""
from __future__ import annotations

from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext


_INSTRUCTION = """\
## Extended Thinking

Before producing your final response, write your private reasoning
inside `<think>...</think>` tags. The contents of these tags are
NEVER shown to the user directly — they are routed to a separate
"reasoning" panel that the user can expand on demand.

Rules:
1. Use the tags ONLY for your private chain-of-thought reasoning.
2. Place the `<think>` block(s) BEFORE your visible response, not
   inside code blocks or other formatting.
3. Always close `<think>` with `</think>`. Multiple separate blocks
   per turn are fine.
4. Keep visible output (outside the tags) clean and final — the user
   does not need to see meta-commentary about your reasoning.
5. NEVER use these tags inside code blocks, examples, or other
   contexts where `<think>` would be legitimate content. They are
   reserved exclusively for your private reasoning.
"""


class ThinkingInjector(DynamicInjectionProvider):
    """Injects the ``<think>``-tag instruction for non-native-thinking
    providers."""

    priority = 60  # plan=10, yolo=20, custom modes 50+; thinking 60

    @property
    def provider_id(self) -> str:
        return "thinking_tags_fallback"

    async def collect(self, ctx: InjectionContext) -> str | None:
        effort = str(
            ctx.runtime.custom.get("reasoning_effort", "medium")
        ).lower()
        if effort == "none":
            return None
        # Default to False (fallback active) when CLI hasn't wired the
        # capability flag — model-agnostic visibility wins on tie.
        native = bool(
            ctx.runtime.custom.get("_provider_supports_native_thinking", False)
        )
        if native:
            return None
        return _INSTRUCTION


__all__ = ["ThinkingInjector"]
```

- [ ] **Step 4:** Run tests

```bash
pytest tests/test_thinking_injector.py -v
```

Expected: 7 passed.

- [ ] **Step 5:** Commit

```bash
git add opencomputer/agent/thinking_injector.py tests/test_thinking_injector.py
git commit -m "feat(agent): ThinkingInjector for non-native-thinking providers"
```

---

## Task B5: Wire parser + injector into the loop, conditionally

**Files:**
- Modify: `opencomputer/agent/loop.py` (wrap stream when fallback active)
- Modify: `opencomputer/cli.py` (stash capability flag + register injector)

- [ ] **Step 1:** In `cli.py`, right after the runtime + reasoning_store init:

```python
    # Phase B: wire model-agnostic thinking. Stash the active provider's
    # native-thinking capability on the runtime so the ThinkingInjector
    # (system-prompt) and the loop's stream wrapper can decide whether
    # to activate the prompt-based fallback.
    try:
        _caps = provider.capabilities()
        runtime.custom["_provider_supports_native_thinking"] = bool(
            getattr(_caps, "supports_native_thinking", False)
        )
    except Exception:  # noqa: BLE001 — never crash on capability sniff
        runtime.custom["_provider_supports_native_thinking"] = False
```

Then register the injector with the InjectionEngine. Find where other injectors are registered (search for `injection_engine.register(` or similar):

```bash
grep -n "InjectionEngine\|injection.register\|register.*Injector" opencomputer/cli.py | head
```

Add:

```python
    from opencomputer.agent.injection import engine as injection_engine
    from opencomputer.agent.thinking_injector import ThinkingInjector
    # Registration is idempotent per provider_id — if already registered
    # (test re-runs, double-init), unregister first to avoid the
    # InjectionEngine.register's "already registered" ValueError.
    injection_engine.unregister("thinking_tags_fallback")
    injection_engine.register(ThinkingInjector())
```

The `injection_engine` is a module-level singleton at `opencomputer/agent/injection.py:106` (`engine = InjectionEngine()`). Already imported by loop.py — same import shape works in cli.py.

- [ ] **Step 2:** In `loop.py`, the call site is at **line 2885** (`async for event in self.provider.stream_complete(...)`). Find any sibling sites:

```bash
grep -n "stream_complete\|async for event in" opencomputer/agent/loop.py | head -10
```

At each such site (likely just one), wrap the source like this (pseudo-diff):

```python
    stream_source = provider.stream_complete(...)
    # Phase B: when the provider doesn't have native thinking AND the
    # user has effort > none, transparently extract <think>...</think>
    # tags from text deltas and re-emit as thinking_delta events. The
    # ThinkingInjector adds the matching system-prompt instruction.
    runtime_extras_eff = runtime_extras.get("reasoning_effort", "medium")
    if (
        runtime_extras_eff != "none"
        and not bool(runtime_extras.get("_provider_supports_native_thinking", False))
    ):
        from opencomputer.agent.thinking_parser import ThinkingTagsParser
        stream_source = ThinkingTagsParser().wrap(stream_source)
    async for event in stream_source:
        ...existing logic...
```

(Adjust to match the actual variable names and call sites. There may be 2-3 call sites — wrap them all consistently. Consider extracting a helper `_maybe_wrap_for_thinking(source, runtime_extras)` to DRY.)

- [ ] **Step 3:** Add an integration test — see Task B6.

- [ ] **Step 4:** Smoke test — full repo

```bash
pytest tests/ -q 2>&1 | tail -3
```

Expected: green plus the new tests from Tasks B1-B4.

- [ ] **Step 5:** Commit

```bash
git add opencomputer/agent/loop.py opencomputer/cli.py
git commit -m "feat(loop): wire ThinkingTagsParser + ThinkingInjector for non-native providers"
```

---

## Task B6: Cross-provider end-to-end tests

**Files:**
- Create: `tests/test_thinking_cross_provider.py`

This test uses a fake provider with `supports_native_thinking=False`, drives a fake stream that includes `<think>...</think>`, and verifies the loop's `thinking_callback` receives the contents while `stream_callback` receives the cleaned text.

- [ ] **Step 1:** Write the test

```python
# tests/test_thinking_cross_provider.py
"""End-to-end: a non-native provider's text stream containing
<think>...</think> tags ends up routed through thinking_callback,
identical to what an Anthropic native-thinking provider produces.

This is the key contract for "model-agnostic extended thinking".
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from opencomputer.agent.thinking_parser import ThinkingTagsParser
from plugin_sdk.provider_contract import StreamEvent


async def _fake_stream(*chunks: str) -> AsyncIterator[StreamEvent]:
    for c in chunks:
        yield StreamEvent(kind="text_delta", text=c)
    yield StreamEvent(kind="done")


def test_full_flow_non_native_provider_emits_thinking_via_parser():
    """Simulates the loop's stream-consumption path. The parser is
    inserted as a wrapper; thinking_callback receives the contents of
    <think> blocks; stream_callback receives the cleaned visible text.
    """
    text_chunks: list[str] = []
    thinking_chunks: list[str] = []

    async def consume():
        wrapped = ThinkingTagsParser().wrap(
            _fake_stream(
                "Sure! <think>let me ",
                "think about this</th",
                "ink> The answer is ",
                "<think>actually 41</think>42.",
            )
        )
        async for ev in wrapped:
            if ev.kind == "text_delta":
                text_chunks.append(ev.text)
            elif ev.kind == "thinking_delta":
                thinking_chunks.append(ev.text)

    asyncio.run(consume())

    text = "".join(text_chunks)
    thinking = "".join(thinking_chunks)
    # Visible text is clean, no tags.
    assert "<think>" not in text and "</think>" not in text
    assert text == "Sure!  The answer is 42."
    # Thinking captured both blocks.
    assert thinking == "let me think about thisactually 41"


def test_full_flow_with_native_provider_uses_existing_path():
    """When the provider already emits thinking_delta natively, the
    parser is NOT inserted (cli.py decides this). Verify the parser
    is a no-op on a stream that already has thinking_delta events —
    they pass through verbatim.
    """
    parser = ThinkingTagsParser()

    async def native_stream():
        yield StreamEvent(kind="text_delta", text="Hello ")
        yield StreamEvent(kind="thinking_delta", text="I think...")
        yield StreamEvent(kind="text_delta", text="world.")
        yield StreamEvent(kind="done")

    async def consume():
        out = []
        async for ev in parser.wrap(native_stream()):
            out.append(ev)
        return out

    out = asyncio.run(consume())
    # The native thinking_delta passes through unchanged (parser only
    # inspects text_delta events).
    thinking = "".join(e.text for e in out if e.kind == "thinking_delta")
    text = "".join(e.text for e in out if e.kind == "text_delta")
    assert thinking == "I think..."
    assert text == "Hello world."
```

- [ ] **Step 2:** Run

```bash
pytest tests/test_thinking_cross_provider.py -v
```

Expected: 2 passed.

- [ ] **Step 3:** Run the FULL suite to catch any unintended interactions

```bash
pytest tests/ -q 2>&1 | tail -5
```

Expected: full green.

- [ ] **Step 4:** Lint

```bash
ruff check opencomputer/ plugin_sdk/ extensions/ tests/ 2>&1 | tail -5
ruff check --fix opencomputer/ plugin_sdk/ extensions/ tests/  # if any auto-fixable
```

- [ ] **Step 5:** Commit

```bash
git add tests/test_thinking_cross_provider.py
git commit -m "test: cross-provider thinking E2E (parser flow + native passthrough)"
```

---

## Task B7: CHANGELOG + push + PR

- [ ] **Step 1:** Update CHANGELOG

In `OpenComputer/CHANGELOG.md` under `[Unreleased]` → `### Added`:

```markdown
### Added — Model-Agnostic Extended Thinking

Extended thinking now works on EVERY provider, not just Anthropic + OpenAI o-series. New `ProviderCapabilities.supports_native_thinking: bool` declares whether a provider already emits `thinking_delta` events natively. For providers that don't (gpt-4o, OpenRouter routes to non-thinking models, Bedrock proxies, local Llama via OpenAI-compatible APIs, etc.), a new `ThinkingInjector` adds a system-prompt instruction telling the model to wrap its reasoning in `<think>...</think>` tags, and a new `ThinkingTagsParser` transparently extracts those tags from the text stream and re-emits them as `thinking_delta` events. The existing `thinking_callback` → `StreamingRenderer` → `ReasoningStore` pipeline (PR #382) picks them up unchanged — `/reasoning show` works on any model.
```

- [ ] **Step 2:** Push + PR

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): model-agnostic extended thinking"
git push -u origin feat/model-agnostic-thinking
gh pr create --title "feat: model-agnostic extended thinking via <think>-tag fallback" \
  --body "$(cat <<'EOF'
## Summary
- Extended thinking on EVERY provider, not just Anthropic + OpenAI o-series.
- `ProviderCapabilities.supports_native_thinking: bool` declares native support; Anthropic + OpenAI (o-series only) opt in. Other providers default False → fallback activates.
- `ThinkingInjector` (DynamicInjectionProvider) adds a `<think>`-tag instruction to the system prompt when the active provider lacks native thinking AND `reasoning_effort != "none"`.
- `ThinkingTagsParser` (async stream wrapper) extracts the tag contents from text deltas — handles chunk-boundary splits, multiple blocks per response, unclosed-tag flush — and emits synthetic `thinking_delta` events.
- Existing `thinking_callback` → `StreamingRenderer` → `ReasoningStore` chain (PR #382) picks them up unchanged; `/reasoning show` works on any model.

## Architecture
- `cli.py` reads `provider.capabilities().supports_native_thinking` at session start and stashes it on `runtime.custom["_provider_supports_native_thinking"]`.
- `InjectionEngine` registers `ThinkingInjector`; it gates on the runtime flag.
- `loop.py` wraps `provider.stream_complete()` with `ThinkingTagsParser().wrap(...)` only when the flag is False AND effort > "none".

## Test plan
- [x] `tests/test_provider_capabilities_supports_native_thinking.py` — 3 tests
- [x] `tests/test_anthropic_provider_native_thinking.py` + `test_openai_provider_native_thinking.py` — capability values
- [x] `tests/test_thinking_parser.py` — 10 tests (chunk splits, multiple blocks, unclosed, non-text passthrough, empty block, partial buffer flush)
- [x] `tests/test_thinking_injector.py` — 7 tests (priority, native True/False, effort none, default-effort, missing flag)
- [x] `tests/test_thinking_cross_provider.py` — 2 tests (full flow, native passthrough)

## Manual verification
```
opencomputer
> /reasoning medium     # ensure effort is set
> what's 2+2?           # using a non-native model (gpt-4o)
# Expect: collapsed line shows turn id + action count
> /reasoning show
# Expect: tree with thinking text (the model's <think> block, parsed)
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Checklist

After both phases complete:

1. **Spec coverage:**
   - "AskUserQuestion never stops to answer" → Phase A handler routes through Rich, no more stdin conflict ✓
   - "Extended thinking model-agnostic, not just Claude API" → Phase B fallback covers every provider with `supports_native_thinking=False` ✓

2. **Backwards compat:**
   - AskUserQuestion stdin path still works (headless / piped input) ✓
   - Anthropic native thinking unchanged (capability=True → injector skipped, parser skipped) ✓
   - OpenAI o-series native thinking unchanged ✓
   - Default `ProviderCapabilities()` semantics unchanged (new field defaults False) ✓

3. **Edge cases covered:**
   - Tag split mid-token across chunks ✓
   - Multiple thinking blocks per response ✓
   - Unclosed `<think>` (model omits `</think>`) ✓
   - Empty thinking block ✓
   - Effort=none disables fallback ✓
   - Missing `_provider_supports_native_thinking` flag → safe default to fallback ✓
   - Tool/done events pass through parser untouched ✓
   - Ctrl+C during AskUserQuestion → clean error ✓

4. **No placeholders.** Every step has real code, real tests, real commands. ✓

5. **Two PRs.** Phase A first (small, urgent bug). Phase B second (medium feature on its own branch from main). ✓

---

## Audit log — issues caught and resolved

This plan was self-audited as an expert critic before execution. Issues found and fixed inline:

| # | Issue | Resolution |
|---|---|---|
| 1 | `sys.stdin.readline()` competes with prompt_toolkit/Rich.Live for stdin → infinite hang. | Phase A handler uses `console.input()` after stopping Live. |
| 2 | Tools have no native runtime access; how do they reach the handler? | ContextVar in `plugin_sdk/interaction.py` — async-safe, scope-friendly, no SDK breakage. |
| 3 | `<think>` could appear in legit code/text discussions (false positives). | System prompt explicitly forbids it inside code blocks; parser only enters thinking-mode after explicit `<think>` (a stray `</think>` in code is harmless). |
| 4 | `<think>` tags split across stream chunks would break naive substring search. | Parser holds back a tag-length suffix per chunk; concatenates with next chunk before scanning. |
| 5 | Models might never close `<think>` — orphaned content lost. | Parser flushes remaining buffer as thinking on `done` event. |
| 6 | `OpenAI` reports native thinking, but only o-series have it; gpt-4o would be misclassified. | Capability is model-dependent: `_is_native_reasoning_model(model)` check. |
| 7 | `InjectionContext` doesn't carry provider info. | Stash `_provider_supports_native_thinking` flag on `runtime.custom` at session start (matches existing `_reasoning_store` pattern from PR #382). |
| 8 | When `_provider_supports_native_thinking` flag is missing (wire path / tests), fallback should default ON (model-agnostic visibility wins on tie). | Injector + parser default to "fallback active" when the flag is absent. |
| 9 | Rich.Live restart after AskUserQuestion handler returns — who triggers it? | Renderer creates Live lazily on next `start_thinking` / `_refresh`. The handler just stops Live; the next stream tick recreates it. No explicit restart needed. |
| 10 | `ProviderCapabilities` is frozen + slots — does adding a field break existing kwargs callers? | New field has a default; existing `ProviderCapabilities(...)` calls keep working unchanged. Verified via `test_existing_capabilities_unaffected`. |
| 11 | Two phases in one plan — should they be one PR or two? | TWO PRs (different worktrees, different branches, different concerns). Plan is one document but execution is sequential PRs. |
| 12 | OpenAI's `_REASONING_MODEL_PREFIXES` will go stale as new series ship. | Add a comment + keep the list short; users on bleeding-edge models can override via capability monkey-patch in their own provider plugin until we update. Acceptable maintenance burden. |
| 13 | **Anthropic capability — initially planned as unconditionally True, but legacy models (Opus 3.5, Sonnet 3.5) lack native thinking.** Setting True for them would silently disable the fallback → those users see NO thinking. | Use existing `supports_adaptive_thinking(self.model)` helper (already imported at provider.py:31). For legacy models capability is False → fallback activates. ✓ |
| 14 | **InjectionEngine.register() raises on duplicate `provider_id`** — re-init / test re-runs would crash. | Plan now `unregister(...)` first then `register(...)` for idempotency. ✓ |
| 15 | **Loop wrap site location** unclear in initial plan. | Verified at `loop.py:2885` (`async for event in self.provider.stream_complete(...)`). Single site for the wrap. ✓ |
| 16 | **Parser hold-back stutter** — when chunk ends with `<th` (potential tag start), the parser holds those bytes back. User sees a small visual delay at chunk boundaries. | Acceptable: chunks are typically 50+ chars and arrive at ~10-100ms intervals. The held-back bytes (≤7 for open tag, ≤8 for close tag) are emitted with the next chunk. Documented in the parser docstring. ✓ |
| 17 | **`<` operator collisions** — Python code like `if x < think_max:` could trigger partial-tag matching. | Parser conservatively only holds back if the trailing portion EXACTLY matches a non-empty prefix of `<think>`. `<th_` is not a `<think>` prefix, so the parser emits it as text immediately. Unit-tested via the multi-block / split-tag tests. ✓ |
| 18 | **Native-passthrough verification gap** — does the parser correctly forward `thinking_delta` events that an Anthropic provider emits natively when capabilities() reports `supports_native_thinking=True`? In that case the parser SHOULDN'T even be wired in (loop skips wrapping). But defense-in-depth: if the parser gets wrapped around a native stream by mistake, it should pass `thinking_delta` events through unchanged. | Parser's first check is `if event.kind != "text_delta"` — non-text events including `thinking_delta` pass through verbatim. Covered by `test_full_flow_with_native_provider_uses_existing_path`. ✓ |

## Execution Handoff

Plan saved to `OpenComputer/docs/superpowers/plans/2026-05-03-ask-user-question-and-thinking.md`. Two PRs to ship sequentially via `superpowers:executing-plans`.
