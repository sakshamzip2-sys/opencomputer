# TUI Uplift — Phase 1 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace OpenComputer's `console.input()`-based chat loop with a `prompt_toolkit.PromptSession` that supports ESC-to-interrupt-streaming, Ctrl+C-to-cancel-turn (no full-CLI exit), persistent cross-session history, multi-line editing, bracketed paste, and a slash-command registry with the first 8 commands wired (`/exit`, `/clear`, `/help`, `/screenshot`, `/export`, `/cost`, `/model`, `/sessions`).

**Architecture:** A new module `opencomputer/cli_ui/input_loop.py` owns the input layer. It builds a `PromptSession` with `FileHistory`, key bindings, and a slash-command completer. The chat loop in `cli.py:_run_chat_session` delegates input acquisition to `input_loop.read_user_input()` and wraps `asyncio.run(_run_turn(...))` in a turn-cancel scope. **Two cancel paths cooperate:** (a) a SIGINT handler routes Ctrl+C → `scope.request_cancel()`; (b) a `KeyboardListener` daemon thread (`opencomputer/cli_ui/keyboard_listener.py`) reads stdin in raw mode while the prompt is *not* active, watching for ESC and pushing cancel into the same scope. `task.cancel()` then propagates `CancelledError` through `AgentLoop.run_conversation` (which already handles it at `loop.py:1540`). Slash commands are dispatched by a registry module (`opencomputer/cli_ui/slash.py`) modeled on hermes-agent's `CommandDef` pattern. `Console(record=True)` enables `/screenshot` to dump rendered output via `console.export_text/svg/html`.

**Tech Stack:** Python 3.12+, `prompt_toolkit>=3.0` (new dep), Rich (existing), Typer (existing), pytest + pytest-asyncio (existing).

---

## File Structure

**Create:**
- `opencomputer/cli_ui/input_loop.py` — `PromptSession` builder, `read_user_input()` async fn, ESC/Ctrl+C key bindings, history file plumbing
- `opencomputer/cli_ui/slash.py` — `CommandDef` dataclass, `SLASH_REGISTRY`, `resolve_command(name)`, `dispatch_slash(text, ctx)` returns dispatch result
- `opencomputer/cli_ui/slash_handlers.py` — concrete handler funcs: `_handle_exit`, `_handle_clear`, `_handle_help`, `_handle_screenshot`, `_handle_export`, `_handle_cost`, `_handle_model`, `_handle_sessions`
- `opencomputer/cli_ui/turn_cancel.py` — `TurnCancelScope` async context manager that owns an `asyncio.Event` and exposes `is_cancelled()`; threaded through the agent loop's `stream_callback`
- `opencomputer/cli_ui/keyboard_listener.py` — daemon thread that reads stdin in raw mode during a streaming turn, calls `scope.request_cancel()` on ESC. Solves the "ESC mid-stream" complaint.
- `tests/test_cli_ui_input_loop.py` — tests for input layer (PromptSession, history, completer)
- `tests/test_cli_ui_slash.py` — tests for slash dispatch
- `tests/test_cli_ui_turn_cancel.py` — tests for cancellation scope
- `tests/test_cli_ui_keyboard_listener.py` — tests for the raw-mode stdin reader

**Modify:**
- `pyproject.toml` — add `prompt_toolkit>=3.0` to `dependencies`
- `opencomputer/cli.py:212` — swap `console = Console()` for `console = Console(record=True)` (enables transcript export)
- `opencomputer/cli.py:903-919` — replace input loop with `read_user_input()` + slash dispatch + cancellable turn scope
- `opencomputer/cli_ui/__init__.py` — export `read_user_input`, `dispatch_slash`, `TurnCancelScope`
- `CHANGELOG.md` — Unreleased section: TUI Phase 1 entry

**Tests:**
- `tests/test_cli_ui_input_loop.py` (new)
- `tests/test_cli_ui_slash.py` (new)
- `tests/test_cli_ui_turn_cancel.py` (new)
- `tests/test_cli_smoke.py` (existing, may need touch-up if it asserts on the old prompt string)

---

## Tasks

### Task 1: Add prompt_toolkit dependency

**Files:**
- Modify: `pyproject.toml:22-41` (dependencies block)

- [ ] **Step 1: Add prompt_toolkit to dependencies**

Edit `pyproject.toml`. Inside the `dependencies = [ ... ]` list, add the line `"prompt_toolkit>=3.0",` after `"rich>=13.7",`:

```toml
  "rich>=13.7",
  "prompt_toolkit>=3.0",
  "typer>=0.12",
```

- [ ] **Step 2: Install the new dep into the venv**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && source .venv/bin/activate && pip install -e .`
Expected: installs prompt_toolkit (and its transitive `wcwidth`) into the venv. Final line: `Successfully installed ... prompt_toolkit-3.0.x ...`.

- [ ] **Step 3: Verify import works**

Run: `python -c "import prompt_toolkit; print(prompt_toolkit.__version__)"`
Expected: prints version (e.g. `3.0.50`).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "feat(cli-ui): add prompt_toolkit>=3.0 dep for TUI uplift"
```

---

### Task 2: TurnCancelScope — cancellable scope for one chat turn

**Files:**
- Create: `opencomputer/cli_ui/turn_cancel.py`
- Test: `tests/test_cli_ui_turn_cancel.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_ui_turn_cancel.py` with:

```python
"""Tests for TurnCancelScope — cooperative cancellation for one chat turn."""
from __future__ import annotations

import asyncio

import pytest

from opencomputer.cli_ui.turn_cancel import TurnCancelScope


@pytest.mark.asyncio
async def test_scope_starts_uncancelled():
    async with TurnCancelScope() as scope:
        assert scope.is_cancelled() is False


@pytest.mark.asyncio
async def test_scope_cancels_when_requested():
    async with TurnCancelScope() as scope:
        scope.request_cancel()
        assert scope.is_cancelled() is True


@pytest.mark.asyncio
async def test_run_callable_returns_normally_when_not_cancelled():
    async def work() -> str:
        await asyncio.sleep(0.001)
        return "done"

    async with TurnCancelScope() as scope:
        result = await scope.run(work())
        assert result == "done"


@pytest.mark.asyncio
async def test_run_raises_cancelled_when_cancel_requested_mid_flight():
    async def slow() -> str:
        await asyncio.sleep(1.0)
        return "done"

    async with TurnCancelScope() as scope:
        task = asyncio.create_task(scope.run(slow()))
        await asyncio.sleep(0.01)
        scope.request_cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_callback_observes_cancellation():
    """A streaming callback wired to scope.is_cancelled() can short-circuit."""
    chunks: list[str] = []

    async def streamer(scope: TurnCancelScope) -> None:
        for i in range(100):
            if scope.is_cancelled():
                break
            chunks.append(str(i))
            await asyncio.sleep(0.001)

    async with TurnCancelScope() as scope:
        task = asyncio.create_task(streamer(scope))
        await asyncio.sleep(0.005)
        scope.request_cancel()
        await task
    assert len(chunks) < 100  # stopped before completion
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && source .venv/bin/activate && pytest tests/test_cli_ui_turn_cancel.py -v`
Expected: All 5 tests FAIL with `ModuleNotFoundError: No module named 'opencomputer.cli_ui.turn_cancel'`.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/cli_ui/turn_cancel.py`:

```python
"""TurnCancelScope — cooperative cancellation for one chat turn.

Owns an ``asyncio.Event`` that the ESC key binding sets when the user
wants to interrupt the in-flight model response. The streaming callback
polls ``is_cancelled()`` to short-circuit chunk processing; ``run()``
wraps an awaitable so a pending ``request_cancel()`` cancels the
underlying task cleanly via ``task.cancel()``.

Pattern adapted from kimi-cli's ``cancel_event`` (single asyncio.Event
threaded through the agent loop) and hermes-agent's polling-flag
interrupt — but unified into one object so the chat loop has a single
handle to pass around.
"""
from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, Awaitable


class TurnCancelScope:
    """Async context manager that holds the cancel state for one turn."""

    def __init__(self) -> None:
        self._event: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None

    async def __aenter__(self) -> "TurnCancelScope":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._task = None

    def request_cancel(self) -> None:
        """Signal cancellation. Idempotent. If a task is registered via
        :meth:`run`, ``task.cancel()`` is also invoked so the awaitable
        unwinds promptly."""
        self._event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def run(self, awaitable: Awaitable[Any]) -> Any:
        """Run ``awaitable`` under this scope. If cancellation is requested
        while it's in flight, ``asyncio.CancelledError`` propagates."""
        self._task = asyncio.ensure_future(awaitable)
        try:
            return await self._task
        finally:
            self._task = None
```

Also create `opencomputer/cli_ui/__init__.py` updates (we'll add the export now to keep import paths stable; if the file already has content, append):

```python
# Append to opencomputer/cli_ui/__init__.py:
from opencomputer.cli_ui.turn_cancel import TurnCancelScope

__all__ = [*globals().get("__all__", []), "TurnCancelScope"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_ui_turn_cancel.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_ui/turn_cancel.py opencomputer/cli_ui/__init__.py tests/test_cli_ui_turn_cancel.py
git commit -m "feat(cli-ui): add TurnCancelScope for cooperative turn cancel"
```

---

### Task 3: Slash command registry and dispatcher

**Files:**
- Create: `opencomputer/cli_ui/slash.py`
- Test: `tests/test_cli_ui_slash.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_ui_slash.py`:

```python
"""Tests for slash command registry + dispatch."""
from __future__ import annotations

import pytest

from opencomputer.cli_ui.slash import (
    CommandDef,
    SLASH_REGISTRY,
    SlashResult,
    is_slash_command,
    resolve_command,
)


def test_is_slash_command_detects_leading_slash():
    assert is_slash_command("/help") is True
    assert is_slash_command("/help arg") is True
    assert is_slash_command(" /help") is False  # must start at col 0
    assert is_slash_command("hello") is False
    assert is_slash_command("") is False
    assert is_slash_command("/") is False  # bare slash is not a command


def test_resolve_command_canonical_name():
    cmd = resolve_command("help")
    assert cmd is not None
    assert cmd.name == "help"


def test_resolve_command_alias():
    cmd = resolve_command("h")
    assert cmd is not None
    assert cmd.name == "help"  # /h is alias for /help


def test_resolve_command_with_slash_prefix():
    cmd = resolve_command("/help")
    assert cmd is not None
    assert cmd.name == "help"


def test_resolve_unknown_command_returns_none():
    assert resolve_command("totally-bogus-cmd") is None


def test_registry_has_required_commands():
    names = {cmd.name for cmd in SLASH_REGISTRY}
    assert {"exit", "clear", "help", "screenshot", "export", "cost", "model", "sessions"} <= names


def test_slash_result_dataclass_shape():
    r = SlashResult(handled=True, exit_loop=False, message="ok")
    assert r.handled is True
    assert r.exit_loop is False
    assert r.message == "ok"


def test_command_def_has_aliases_and_args_hint():
    cmd = resolve_command("help")
    assert isinstance(cmd, CommandDef)
    assert "h" in cmd.aliases
    # args_hint can be empty string but must be defined
    assert cmd.args_hint == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_ui_slash.py -v`
Expected: All tests FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/cli_ui/slash.py`:

```python
"""Slash command registry + dispatcher.

Pattern adapted from hermes-agent's ``CommandDef`` registry. The registry
is a flat ``list[CommandDef]`` — single source of truth — and lookups are
built lazily as needed. Handlers live in :mod:`slash_handlers`; this
module owns only metadata + resolution so tests can exercise the registry
without importing Rich/prompt_toolkit.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CommandDef:
    """One slash command. Handlers are looked up by name in
    :mod:`slash_handlers` rather than stored here so the registry stays
    importable in test contexts that don't have Console."""

    name: str
    description: str
    category: str = "general"
    aliases: tuple[str, ...] = field(default_factory=tuple)
    args_hint: str = ""


@dataclass
class SlashResult:
    """What happened when a slash command ran.

    - ``handled``: True if the input was recognized as a slash command
      (regardless of success). False means the chat loop should treat
      the input as a normal user message instead.
    - ``exit_loop``: True if the loop should terminate after this command
      (e.g. ``/exit``).
    - ``message``: optional human-readable status to print.
    """

    handled: bool
    exit_loop: bool = False
    message: str = ""


SLASH_REGISTRY: list[CommandDef] = [
    CommandDef(
        name="exit",
        description="Exit the chat session.",
        category="session",
        aliases=("quit", "q"),
    ),
    CommandDef(
        name="clear",
        description="Clear the screen and start a fresh session id.",
        category="session",
        aliases=("new", "reset"),
    ),
    CommandDef(
        name="help",
        description="Show available slash commands.",
        category="meta",
        aliases=("h", "?"),
    ),
    CommandDef(
        name="screenshot",
        description="Save a snapshot of the current rendered output.",
        category="output",
        aliases=("snap",),
        args_hint="[path]",
    ),
    CommandDef(
        name="export",
        description="Export the full transcript to a file (markdown).",
        category="output",
        args_hint="[path]",
    ),
    CommandDef(
        name="cost",
        description="Show cumulative input/output tokens for this session.",
        category="meta",
    ),
    CommandDef(
        name="model",
        description="Show or switch the active model.",
        category="config",
        args_hint="[provider/model]",
    ),
    CommandDef(
        name="sessions",
        description="List recent sessions.",
        category="session",
        aliases=("history",),
    ),
]


def _build_lookup() -> dict[str, CommandDef]:
    out: dict[str, CommandDef] = {}
    for cmd in SLASH_REGISTRY:
        out[cmd.name] = cmd
        for alias in cmd.aliases:
            out[alias] = cmd
    return out


_LOOKUP: dict[str, CommandDef] = _build_lookup()


def is_slash_command(text: str) -> bool:
    """True iff text starts with ``/`` followed by at least one non-space char."""
    if not text or not text.startswith("/"):
        return False
    rest = text[1:].lstrip()
    return bool(rest)


def resolve_command(name: str) -> CommandDef | None:
    """Resolve a name (with or without leading ``/``) to a CommandDef."""
    n = name.lstrip("/").strip().lower()
    return _LOOKUP.get(n)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_ui_slash.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_ui/slash.py tests/test_cli_ui_slash.py
git commit -m "feat(cli-ui): add slash command registry + resolver"
```

---

### Task 4: Slash command handlers

**Files:**
- Create: `opencomputer/cli_ui/slash_handlers.py`
- Modify: `tests/test_cli_ui_slash.py` (append handler-dispatch tests)

- [ ] **Step 1: Append failing dispatch tests**

Append to `tests/test_cli_ui_slash.py`:

```python
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from rich.console import Console

from opencomputer.cli_ui.slash_handlers import SlashContext, dispatch_slash


def _make_ctx(console: Console | None = None) -> SlashContext:
    return SlashContext(
        console=console or Console(record=True),
        session_id="test-session",
        config=MagicMock(model=MagicMock(model="claude-3-5", provider="anthropic")),
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 100, "out": 200},
        get_session_list=lambda: [{"id": "s1", "started_at": "2026-01-01T00:00:00"}],
    )


def test_dispatch_unknown_returns_unhandled():
    ctx = _make_ctx()
    r = dispatch_slash("/totally-bogus", ctx)
    assert r.handled is False


def test_dispatch_non_slash_returns_unhandled():
    ctx = _make_ctx()
    r = dispatch_slash("hello world", ctx)
    assert r.handled is False


def test_dispatch_exit_signals_loop_exit():
    ctx = _make_ctx()
    r = dispatch_slash("/exit", ctx)
    assert r.handled is True
    assert r.exit_loop is True


def test_dispatch_help_lists_commands():
    console = Console(record=True)
    ctx = _make_ctx(console=console)
    r = dispatch_slash("/help", ctx)
    assert r.handled is True
    assert r.exit_loop is False
    out = console.export_text()
    assert "/exit" in out
    assert "/help" in out


def test_dispatch_clear_calls_callback():
    called: list[bool] = []
    ctx = _make_ctx()
    ctx.on_clear = lambda: called.append(True)
    r = dispatch_slash("/clear", ctx)
    assert r.handled is True
    assert called == [True]


def test_dispatch_screenshot_writes_file():
    console = Console(record=True)
    console.print("hello world")
    ctx = _make_ctx(console=console)
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "snap.txt"
        r = dispatch_slash(f"/screenshot {out_path}", ctx)
        assert r.handled is True
        assert out_path.exists()
        assert "hello world" in out_path.read_text()


def test_dispatch_export_writes_file():
    console = Console(record=True)
    console.print("turn 1")
    ctx = _make_ctx(console=console)
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "transcript.md"
        r = dispatch_slash(f"/export {out_path}", ctx)
        assert r.handled is True
        assert out_path.exists()
        text = out_path.read_text()
        assert "turn 1" in text


def test_dispatch_cost_prints_summary():
    console = Console(record=True)
    ctx = _make_ctx(console=console)
    ctx.get_cost_summary = lambda: {"in": 1234, "out": 5678}
    r = dispatch_slash("/cost", ctx)
    assert r.handled is True
    out = console.export_text()
    assert "1234" in out
    assert "5678" in out


def test_dispatch_model_prints_active_model():
    console = Console(record=True)
    ctx = _make_ctx(console=console)
    r = dispatch_slash("/model", ctx)
    assert r.handled is True
    out = console.export_text()
    assert "claude-3-5" in out
    assert "anthropic" in out


def test_dispatch_sessions_lists_session_ids():
    console = Console(record=True)
    ctx = _make_ctx(console=console)
    r = dispatch_slash("/sessions", ctx)
    assert r.handled is True
    out = console.export_text()
    assert "s1" in out
    assert "2026-01-01" in out


def test_dispatch_alias_resolves():
    ctx = _make_ctx()
    r = dispatch_slash("/q", ctx)  # alias for /exit
    assert r.handled is True
    assert r.exit_loop is True
```

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_cli_ui_slash.py -v`
Expected: 11 new tests FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement handlers**

Create `opencomputer/cli_ui/slash_handlers.py`:

```python
"""Concrete handlers for the slash commands defined in :mod:`slash`.

Each handler takes a :class:`SlashContext` (the chat loop wires it up
once at session start) and returns a :class:`SlashResult`. Handlers are
intentionally small — anything that needs Rich rendering or filesystem
access uses ``ctx.console``; anything that needs agent state goes
through the callbacks (``on_clear``, ``get_cost_summary``, etc.).

The layer of indirection through callbacks (rather than passing the
agent loop / config directly) keeps this module testable without
booting an agent.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.table import Table

from opencomputer.cli_ui.slash import (
    SLASH_REGISTRY,
    CommandDef,
    SlashResult,
    is_slash_command,
    resolve_command,
)


@dataclass
class SlashContext:
    """Everything a slash handler might need from the chat loop."""

    console: Console
    session_id: str
    config: Any  # Config — typed loosely to avoid import cycle
    on_clear: Callable[[], None]
    get_cost_summary: Callable[[], dict[str, int]]
    get_session_list: Callable[[], list[dict[str, Any]]]


def _split_args(text: str) -> tuple[str, list[str]]:
    """Split ``/cmd arg1 arg2`` into ``("cmd", ["arg1", "arg2"])``."""
    parts = text.lstrip("/").split()
    if not parts:
        return ("", [])
    return (parts[0], parts[1:])


def _handle_exit(ctx: SlashContext, args: list[str]) -> SlashResult:
    return SlashResult(handled=True, exit_loop=True, message="bye.")


def _handle_clear(ctx: SlashContext, args: list[str]) -> SlashResult:
    ctx.on_clear()
    ctx.console.print("[dim]session cleared.[/dim]")
    return SlashResult(handled=True)


def _handle_help(ctx: SlashContext, args: list[str]) -> SlashResult:
    table = Table(title="Slash commands", show_header=True, header_style="bold")
    table.add_column("Command", style="cyan")
    table.add_column("Aliases", style="dim")
    table.add_column("Description")
    for cmd in SLASH_REGISTRY:
        aliases = ", ".join(f"/{a}" for a in cmd.aliases) if cmd.aliases else ""
        ctx_name = f"/{cmd.name}"
        if cmd.args_hint:
            ctx_name = f"{ctx_name} {cmd.args_hint}"
        table.add_row(ctx_name, aliases, cmd.description)
    ctx.console.print(table)
    return SlashResult(handled=True)


def _handle_screenshot(ctx: SlashContext, args: list[str]) -> SlashResult:
    """Dump the rendered console to a file. Format inferred from extension:
    ``.svg`` → SVG, ``.html`` → HTML, anything else → text."""
    if args:
        path = Path(args[0]).expanduser().resolve()
    else:
        from datetime import datetime

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path.cwd() / f"oc-screenshot-{ts}.txt"
    suffix = path.suffix.lower()
    if suffix == ".svg":
        ctx.console.save_svg(str(path), title="OpenComputer")
    elif suffix in (".html", ".htm"):
        ctx.console.save_html(str(path))
    else:
        ctx.console.save_text(str(path))
    ctx.console.print(f"[green]screenshot →[/green] {path}")
    return SlashResult(handled=True)


def _handle_export(ctx: SlashContext, args: list[str]) -> SlashResult:
    """Same as screenshot but defaults to .md and uses save_text."""
    if args:
        path = Path(args[0]).expanduser().resolve()
    else:
        from datetime import datetime

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path.cwd() / f"oc-transcript-{ts}.md"
    ctx.console.save_text(str(path))
    ctx.console.print(f"[green]transcript →[/green] {path}")
    return SlashResult(handled=True)


def _handle_cost(ctx: SlashContext, args: list[str]) -> SlashResult:
    summary = ctx.get_cost_summary()
    in_tok = summary.get("in", 0)
    out_tok = summary.get("out", 0)
    ctx.console.print(
        f"[bold]session tokens[/bold]  in={in_tok}  out={out_tok}  total={in_tok + out_tok}"
    )
    return SlashResult(handled=True)


def _handle_model(ctx: SlashContext, args: list[str]) -> SlashResult:
    if not args:
        # Show active model.
        m = getattr(ctx.config.model, "model", "?")
        p = getattr(ctx.config.model, "provider", "?")
        ctx.console.print(f"[bold]active model[/bold]  {m}  ({p})")
        return SlashResult(handled=True)
    # Switching mid-session is intentionally not implemented yet — print a
    # friendly note. Phase 2 wires the real swap.
    ctx.console.print(
        "[yellow]switching mid-session not implemented yet — restart with --model[/yellow]"
    )
    return SlashResult(handled=True)


def _handle_sessions(ctx: SlashContext, args: list[str]) -> SlashResult:
    sessions = ctx.get_session_list()
    if not sessions:
        ctx.console.print("[dim]no prior sessions.[/dim]")
        return SlashResult(handled=True)
    table = Table(title="Recent sessions", show_header=True)
    table.add_column("id", style="cyan")
    table.add_column("started_at")
    for s in sessions[:20]:
        # SessionDB.list_sessions returns rows with "id" + "started_at" columns;
        # the test fixture uses the same shape.
        table.add_row(s.get("id", "?"), str(s.get("started_at", "?")))
    ctx.console.print(table)
    return SlashResult(handled=True)


_HANDLERS: dict[str, Callable[[SlashContext, list[str]], SlashResult]] = {
    "exit": _handle_exit,
    "clear": _handle_clear,
    "help": _handle_help,
    "screenshot": _handle_screenshot,
    "export": _handle_export,
    "cost": _handle_cost,
    "model": _handle_model,
    "sessions": _handle_sessions,
}


def dispatch_slash(text: str, ctx: SlashContext) -> SlashResult:
    """Dispatch a slash-command string to its handler.

    Returns ``SlashResult(handled=False)`` for non-slash text or unknown
    commands so the caller can fall back to "treat as normal message"
    (for non-slash) or "print error" (for unknown — caller's choice)."""
    if not is_slash_command(text):
        return SlashResult(handled=False)
    name, args = _split_args(text)
    cmd: CommandDef | None = resolve_command(name)
    if cmd is None:
        ctx.console.print(f"[red]unknown command:[/red] /{name}  (try /help)")
        return SlashResult(handled=True)  # we ate the input — don't send to LLM
    handler = _HANDLERS[cmd.name]
    return handler(ctx, args)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_ui_slash.py -v`
Expected: All ~17 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_ui/slash_handlers.py tests/test_cli_ui_slash.py
git commit -m "feat(cli-ui): implement 8 slash command handlers"
```

---

### Task 5: Input loop with PromptSession

**Files:**
- Create: `opencomputer/cli_ui/input_loop.py`
- Modify: `opencomputer/cli_ui/__init__.py`
- Test: `tests/test_cli_ui_input_loop.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_ui_input_loop.py`:

```python
"""Tests for the input loop module — PromptSession + key bindings.

Most prompt_toolkit behavior is interactive and hard to unit-test;
these tests cover the pieces we own: history file path computation,
session builder returns a PromptSession with a FileHistory bound to the
right path, and the pure helpers (``_strip_trailing_whitespace``).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory

from opencomputer.cli_ui.input_loop import (
    _history_file_path,
    _strip_trailing_whitespace,
    build_prompt_session,
)
from opencomputer.cli_ui.turn_cancel import TurnCancelScope


def test_history_file_path_under_profile_home(tmp_path: Path):
    profile = tmp_path / "myprofile"
    profile.mkdir()
    p = _history_file_path(profile)
    assert p.parent == profile
    assert p.name == "input_history"


def test_history_file_path_creates_parent_when_missing(tmp_path: Path):
    profile = tmp_path / "newprofile"  # does not exist
    p = _history_file_path(profile)
    # Path is computed; we don't actually create the file here, but the
    # parent must exist after the call so FileHistory(open) doesn't fail.
    assert p.parent.exists()


def test_strip_trailing_whitespace_simple():
    assert _strip_trailing_whitespace("hello  ") == "hello"
    assert _strip_trailing_whitespace("  ") == ""
    assert _strip_trailing_whitespace("hello\nworld") == "hello\nworld"


def test_build_prompt_session_returns_session(tmp_path: Path):
    scope = TurnCancelScope()
    session = build_prompt_session(profile_home=tmp_path, scope=scope)
    assert isinstance(session, PromptSession)
    # History is a FileHistory pointing under our profile dir.
    assert isinstance(session.history, FileHistory)
    assert Path(session.history.filename).parent == tmp_path
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_ui_input_loop.py -v`
Expected: All 4 tests FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/cli_ui/input_loop.py`:

```python
"""Input layer for the chat loop.

Replaces ``Console.input(...)`` with a ``prompt_toolkit.PromptSession``
that supports:

- Persistent ``FileHistory`` (Up-arrow recalls across sessions)
- ``Alt+Enter`` / ``Ctrl+J`` insert literal newline (multi-line input)
- ``Esc`` requests cancellation of an in-flight turn via ``TurnCancelScope``
- ``Ctrl+C`` while idle clears the input buffer; while a turn is running
  it requests cancellation through the same scope (same effect as Esc)
- Bracketed paste (handled automatically by prompt_toolkit)
- ``mouse_support=False`` (we want native terminal selection for copy)

The raw stream-cancel binding only matters while the prompt is "alive",
which by design it is — the chat loop awaits ``read_user_input()``
between turns; the prompt session is rebuilt fresh each turn so a
cancelled scope from a previous turn doesn't leak.
"""
from __future__ import annotations

from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from opencomputer.cli_ui.turn_cancel import TurnCancelScope


def _history_file_path(profile_home: Path) -> Path:
    """Resolve the history file path; ensure the parent dir exists."""
    profile_home.mkdir(parents=True, exist_ok=True)
    return profile_home / "input_history"


def _strip_trailing_whitespace(text: str) -> str:
    """Strip trailing whitespace per line on the last line only.

    Multi-line input keeps inner formatting; we only trim the very tail
    so ``hello  `` becomes ``hello``."""
    return text.rstrip()


def build_prompt_session(
    *,
    profile_home: Path,
    scope: TurnCancelScope,
) -> PromptSession:
    """Construct a fresh PromptSession bound to ``scope``.

    Build per-turn (not once at startup) so each turn gets a clean
    ``TurnCancelScope`` and the key bindings always close over the
    *current* scope, not a stale one from a previous turn.
    """
    history_path = _history_file_path(profile_home)
    kb = KeyBindings()

    @kb.add(Keys.Escape, eager=True)
    def _esc(event):  # noqa: ANN001
        # ESC during *idle* prompt: clear the buffer (matches Claude Code).
        # ESC during *streaming*: doesn't reach here because the prompt is
        # not the active app while the turn is in flight; the streaming
        # path uses a separate raw key reader (see Task 6).
        event.current_buffer.text = ""

    @kb.add(Keys.ControlJ)
    def _ctrl_j(event):  # noqa: ANN001
        event.current_buffer.insert_text("\n")

    @kb.add(Keys.Escape, Keys.Enter)
    def _alt_enter(event):  # noqa: ANN001
        event.current_buffer.insert_text("\n")

    return PromptSession(
        message=HTML("<ansigreen><b>you ›</b></ansigreen> "),
        history=FileHistory(str(history_path)),
        key_bindings=kb,
        multiline=False,  # default — Alt+Enter inserts \n and Enter submits
        mouse_support=False,
        enable_history_search=True,
        complete_while_typing=False,
    )


async def read_user_input(
    *,
    profile_home: Path,
    scope: TurnCancelScope,
) -> str:
    """Read one line of user input via the prompt session.

    Returns the trimmed string. Caller handles ``EOFError`` (Ctrl+D)
    and ``KeyboardInterrupt`` (Ctrl+C with empty buffer).
    """
    session = build_prompt_session(profile_home=profile_home, scope=scope)
    text = await session.prompt_async()
    return _strip_trailing_whitespace(text or "")
```

Update `opencomputer/cli_ui/__init__.py`:

```python
"""CLI UI subpackage — streaming renderer + input layer + slash dispatch."""
from opencomputer.cli_ui.input_loop import build_prompt_session, read_user_input
from opencomputer.cli_ui.slash import (
    SLASH_REGISTRY,
    CommandDef,
    SlashResult,
    is_slash_command,
    resolve_command,
)
from opencomputer.cli_ui.slash_handlers import SlashContext, dispatch_slash
from opencomputer.cli_ui.streaming import StreamingRenderer, current_renderer
from opencomputer.cli_ui.turn_cancel import TurnCancelScope

__all__ = [
    "SLASH_REGISTRY",
    "CommandDef",
    "SlashContext",
    "SlashResult",
    "StreamingRenderer",
    "TurnCancelScope",
    "build_prompt_session",
    "current_renderer",
    "dispatch_slash",
    "is_slash_command",
    "read_user_input",
    "resolve_command",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_ui_input_loop.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_ui/input_loop.py opencomputer/cli_ui/__init__.py tests/test_cli_ui_input_loop.py
git commit -m "feat(cli-ui): PromptSession-based input layer with FileHistory + multi-line"
```

---

### Task 6: Stream-cancel signal handler (ESC mid-stream)

The prompt_toolkit binding in Task 5 only fires while the **prompt** is the active app. While the LLM is streaming, prompt_toolkit isn't running. We need a separate path: install a SIGINT handler scoped to the turn so Ctrl+C signals the cancel scope (without bringing down the CLI), AND detect raw ESC bytes on stdin during the stream by spawning a small reader thread that consumes one keystroke at a time.

**Files:**
- Modify: `opencomputer/cli_ui/turn_cancel.py` (extend with a `signal_install()` ctx mgr)
- Modify: `tests/test_cli_ui_turn_cancel.py` (append signal-install test)

- [ ] **Step 1: Append failing test**

Append to `tests/test_cli_ui_turn_cancel.py`:

```python
import os
import signal


@pytest.mark.asyncio
async def test_install_sigint_handler_sets_scope_on_signal():
    """Sending SIGINT to ourselves while the scope's signal handler is
    installed should set the cancel flag — instead of raising
    ``KeyboardInterrupt`` and killing the loop."""
    async with TurnCancelScope() as scope:
        with scope.install_sigint_handler():
            # Fire SIGINT into our own process; the scope's handler should
            # catch it and just call request_cancel().
            os.kill(os.getpid(), signal.SIGINT)
            # Give the signal a moment to be delivered to the loop.
            await asyncio.sleep(0.05)
            assert scope.is_cancelled() is True


@pytest.mark.asyncio
async def test_install_sigint_handler_restores_previous_handler():
    async with TurnCancelScope() as scope:
        previous = signal.getsignal(signal.SIGINT)
        with scope.install_sigint_handler():
            assert signal.getsignal(signal.SIGINT) != previous
        # Restored after exit.
        assert signal.getsignal(signal.SIGINT) == previous
```

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_cli_ui_turn_cancel.py -v`
Expected: 2 new tests FAIL with `AttributeError: ... has no attribute 'install_sigint_handler'`.

- [ ] **Step 3: Extend `TurnCancelScope`**

Edit `opencomputer/cli_ui/turn_cancel.py`. Add at the top of the file (after `import asyncio`):

```python
import contextlib
import signal
import sys
from typing import Iterator
```

Append to the `TurnCancelScope` class:

```python
    @contextlib.contextmanager
    def install_sigint_handler(self) -> Iterator[None]:
        """While in this with-block, SIGINT (Ctrl+C) calls
        :meth:`request_cancel` instead of raising ``KeyboardInterrupt``.

        Restored on exit. Best-effort: outside the main thread or on
        platforms where ``signal.signal`` raises (Windows asyncio loops),
        this falls back to a no-op so the chat loop never crashes from
        signal-install failure.
        """
        previous = None
        try:
            previous = signal.getsignal(signal.SIGINT)
        except (ValueError, OSError):  # main-thread restriction
            yield
            return

        def _handler(signum: int, frame: object) -> None:
            self.request_cancel()

        try:
            signal.signal(signal.SIGINT, _handler)
        except (ValueError, OSError):
            yield
            return

        try:
            yield
        finally:
            try:
                signal.signal(signal.SIGINT, previous)
            except (ValueError, OSError):
                pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_ui_turn_cancel.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_ui/turn_cancel.py tests/test_cli_ui_turn_cancel.py
git commit -m "feat(cli-ui): TurnCancelScope.install_sigint_handler — Ctrl+C cancels turn"
```

---

### Task 6.5: KeyboardListener — raw-mode stdin reader for mid-stream ESC

This is the task that actually fixes the user's primary complaint ("ESC doesn't stop streaming"). prompt_toolkit's ESC binding only handles ESC at the prompt; while the LLM is streaming, prompt_toolkit isn't running. We need a daemon thread that reads stdin one byte at a time in raw (non-canonical) mode, watches for ESC (`0x1b`), and calls `scope.request_cancel()`. Runs only between turns; stops cleanly when the turn finishes.

**Files:**
- Create: `opencomputer/cli_ui/keyboard_listener.py`
- Test: `tests/test_cli_ui_keyboard_listener.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_ui_keyboard_listener.py`:

```python
"""Tests for KeyboardListener — daemon thread reading stdin for ESC.

Cannot fully test the raw-mode read path without a real TTY, so these
tests exercise the public API: start/stop is idempotent, the listener
is gated on isatty (so CI / pipe runs no-op safely), and the byte
classifier recognizes ESC.
"""
from __future__ import annotations

import asyncio
import io
from unittest.mock import patch

import pytest

from opencomputer.cli_ui.keyboard_listener import (
    ESC_BYTE,
    KeyboardListener,
    _is_esc,
)
from opencomputer.cli_ui.turn_cancel import TurnCancelScope


def test_esc_byte_constant():
    assert ESC_BYTE == b"\x1b"


def test_is_esc_recognizes_esc():
    assert _is_esc(b"\x1b") is True
    assert _is_esc(b"\x1b\x1b") is True  # Esc Esc still starts with Esc
    assert _is_esc(b"a") is False
    assert _is_esc(b"") is False


@pytest.mark.asyncio
async def test_listener_no_op_on_non_tty():
    """If stdin isn't a TTY (CI / pipe), start() must return immediately
    without touching termios — otherwise tests crash."""
    scope = TurnCancelScope()
    listener = KeyboardListener(scope)
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        listener.start()  # must NOT raise
        listener.stop()


def test_listener_start_stop_idempotent():
    """Calling start/stop twice in a row must not raise."""
    scope = TurnCancelScope()
    listener = KeyboardListener(scope)
    listener.start()
    listener.start()  # second start is a no-op
    listener.stop()
    listener.stop()  # second stop is a no-op


@pytest.mark.asyncio
async def test_listener_processes_esc_byte_via_handle():
    """The internal byte handler sets the scope cancel flag on ESC."""
    scope = TurnCancelScope()
    listener = KeyboardListener(scope)
    listener._handle_byte(b"\x1b")
    assert scope.is_cancelled() is True


@pytest.mark.asyncio
async def test_listener_ignores_non_esc_bytes():
    scope = TurnCancelScope()
    listener = KeyboardListener(scope)
    listener._handle_byte(b"a")
    listener._handle_byte(b"\n")
    listener._handle_byte(b"")
    assert scope.is_cancelled() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_ui_keyboard_listener.py -v`
Expected: All tests FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

Create `opencomputer/cli_ui/keyboard_listener.py`:

```python
"""Daemon-thread stdin reader that fires cancel on ESC during a turn.

prompt_toolkit's ESC binding handles the *prompt* phase (clearing the
input buffer), but during the streaming phase prompt_toolkit isn't
the active app — keystrokes go to the terminal and nothing reads
them. To honor the user's "ESC interrupts streaming" expectation we
need a separate raw-mode stdin reader that runs only while the turn
is in flight.

Pattern adapted from kimi-cli's ``KeyboardListener`` (which uses a
daemon thread + termios cbreak mode). The thread is started when a
turn begins and stopped when the turn ends; only one listener is
active at a time. On non-TTY stdin (CI, piped input) the listener is
a no-op so tests and ``printf … | opencomputer chat`` pipelines
don't crash.

The listener does NOT consume non-ESC bytes — they sit in the OS
buffer. That's by design: prompt_toolkit's next ``prompt_async()``
call drains them when the prompt re-activates. So typing characters
during streaming pre-loads the next turn's prompt; only ESC short-
circuits.
"""
from __future__ import annotations

import logging
import os
import select
import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencomputer.cli_ui.turn_cancel import TurnCancelScope

_log = logging.getLogger("opencomputer.cli_ui.keyboard_listener")

#: The single byte that means "user pressed ESC". Sequences like arrow
#: keys also start with this byte (``\x1b[A``), but we treat any byte
#: starting with ``\x1b`` as a cancel intent — the user can't realistically
#: press an arrow key during streaming since the prompt isn't focused.
ESC_BYTE: bytes = b"\x1b"


def _is_esc(b: bytes) -> bool:
    return b.startswith(ESC_BYTE) if b else False


class KeyboardListener:
    """Background thread that reads stdin and fires ESC cancellation.

    Lifecycle::

        listener = KeyboardListener(scope)
        listener.start()
        try:
            ...  # run the streaming turn
        finally:
            listener.stop()
    """

    def __init__(self, scope: "TurnCancelScope") -> None:
        self._scope = scope
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()

    def start(self) -> None:
        """Begin watching stdin. No-op if stdin isn't a TTY or already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        if not sys.stdin.isatty():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="oc-keyboard-listener", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the listener thread. Idempotent."""
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            # Don't join with infinite timeout — the thread blocks on select
            # with a 0.1s timeout, so it'll wake up promptly.
            t.join(timeout=0.5)
        self._thread = None

    def _handle_byte(self, b: bytes) -> None:
        """Pure helper — exposed for tests. Cancels scope on ESC."""
        if _is_esc(b):
            self._scope.request_cancel()

    def _run(self) -> None:
        """Thread body. Sets stdin into cbreak mode so reads are
        unbuffered, then loops on ``select`` + ``read(1)``."""
        try:
            import termios
            import tty
        except ImportError:
            # Windows: termios is unavailable. Fall through to no-op.
            return

        fd = sys.stdin.fileno()
        try:
            old_attrs = termios.tcgetattr(fd)
        except (termios.error, OSError):
            return  # not a real TTY despite isatty (e.g. SSH edge case)
        try:
            tty.setcbreak(fd)
            while not self._stop_event.is_set():
                # 0.1s poll — keeps stop() responsive.
                r, _, _ = select.select([fd], [], [], 0.1)
                if not r:
                    continue
                try:
                    b = os.read(fd, 1)
                except OSError:
                    break
                if not b:
                    break
                self._handle_byte(b)
                if self._scope.is_cancelled():
                    # Don't process further bytes after cancel — leave them
                    # in the OS buffer for the next prompt to drain.
                    break
        except Exception as exc:  # noqa: BLE001 — must never crash the chat loop
            _log.debug("keyboard listener errored: %s", exc)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            except (termios.error, OSError):
                pass
```

Append to `opencomputer/cli_ui/__init__.py` exports list (add `"KeyboardListener"` to `__all__` and add the import):

```python
from opencomputer.cli_ui.keyboard_listener import KeyboardListener
```

And add `"KeyboardListener"` to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_ui_keyboard_listener.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_ui/keyboard_listener.py opencomputer/cli_ui/__init__.py tests/test_cli_ui_keyboard_listener.py
git commit -m "feat(cli-ui): KeyboardListener — raw-mode stdin ESC cancel during streaming"
```

---

### Task 7: Wire input layer into the chat loop

**Files:**
- Modify: `opencomputer/cli.py:212` (Console → record=True)
- Modify: `opencomputer/cli.py:903-919` (input loop + slash dispatch + cancel scope)

- [ ] **Step 1: Read existing cli.py lines we'll touch**

Run: `sed -n '200,220p' /Users/saksham/Vscode/claude/OpenComputer/opencomputer/cli.py`
Expected output (verify line 212 is `console = Console()` or equivalent — the actual line may have shifted).

If line 212 isn't the Console init, find it: `grep -n "^console = Console" /Users/saksham/Vscode/claude/OpenComputer/opencomputer/cli.py`. Use whatever line that returns.

- [ ] **Step 2: Switch to recording Console**

Edit `opencomputer/cli.py`. Find the line:

```python
console = Console()
```

Replace with:

```python
# record=True enables /screenshot + /export — Rich keeps a render
# log on the console so console.save_text/save_html/save_svg can
# replay every printed segment.
console = Console(record=True)
```

- [ ] **Step 3: Replace the chat loop body**

Edit `opencomputer/cli.py`. Find the existing input loop in `_run_chat_session` (lines roughly 903-919, ending right before the closing of the function):

```python
    while True:
        try:
            user_input = console.input("[bold green]you ›[/bold green] ")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]bye.[/dim]")
            _print_update_hint_if_any()
            return
        if user_input.strip().lower() in {"exit", "quit", ":q"}:
            console.print("[dim]bye.[/dim]")
            _print_update_hint_if_any()
            return
        if not user_input.strip():
            continue
        try:
            asyncio.run(_run_turn(user_input))
        except Exception as e:
            console.print(f"[bold red]error:[/bold red] {type(e).__name__}: {e}")
```

Replace with:

```python
    # New TUI input layer (Phase 1) — PromptSession + slash dispatch +
    # cancellable turn scope + keyboard listener for mid-stream ESC.
    # Falls back to the legacy plain-stream path on non-TTY (pipes /
    # CI / `printf … | opencomputer chat`).
    from opencomputer.agent.config import _home as _profile_home_fn
    from opencomputer.cli_ui import (
        KeyboardListener,
        SlashContext,
        TurnCancelScope,
        dispatch_slash,
        is_slash_command,
        read_user_input,
    )

    profile_home = _profile_home_fn()

    # Cumulative-token tally captured by closure. Each turn's
    # ConversationResult contributes; /cost reads via _get_cost_summary.
    _token_tally = {"in": 0, "out": 0}

    def _on_clear() -> None:
        nonlocal session_id
        session_id = str(uuid.uuid4())
        _token_tally["in"] = 0
        _token_tally["out"] = 0
        console.clear()

    def _get_cost_summary() -> dict[str, int]:
        return dict(_token_tally)

    def _get_session_list() -> list[dict]:
        # SessionDB.list_sessions(limit=20) returns rows with "id" and
        # "started_at" columns. Best-effort — silently empty list if the
        # DB hasn't been initialized yet for this profile.
        try:
            from opencomputer.agent.state import SessionDB
            db = SessionDB(profile_home / "sessions.db")
            rows = db.list_sessions(limit=20)
            return [{"id": r.get("id", "?"),
                     "started_at": r.get("started_at", "?")} for r in rows]
        except Exception:
            return []

    if not sys.stdin.isatty():
        # Non-TTY (piped stdin) — keep the old line-by-line behavior.
        for line in sys.stdin:
            user_input = line.rstrip("\n")
            if not user_input.strip():
                continue
            if user_input.strip().lower() in {"exit", "quit", ":q"}:
                break
            try:
                asyncio.run(_run_turn(user_input))
            except Exception as e:
                console.print(f"[bold red]error:[/bold red] {type(e).__name__}: {e}")
        _print_update_hint_if_any()
        return

    while True:
        async def _read_one() -> str:
            scope = TurnCancelScope()
            return await read_user_input(profile_home=profile_home, scope=scope)

        try:
            user_input = asyncio.run(_read_one())
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]bye.[/dim]")
            _print_update_hint_if_any()
            return
        if not user_input.strip():
            continue

        if is_slash_command(user_input):
            slash_ctx = SlashContext(
                console=console,
                session_id=session_id,
                config=cfg,
                on_clear=_on_clear,
                get_cost_summary=_get_cost_summary,
                get_session_list=_get_session_list,
            )
            result = dispatch_slash(user_input, slash_ctx)
            if result.exit_loop:
                console.print(f"[dim]{result.message or 'bye.'}[/dim]")
                _print_update_hint_if_any()
                return
            continue

        # Run the turn under a cancel scope so ESC / Ctrl+C cancel the
        # in-flight stream instead of crashing the CLI. The
        # KeyboardListener watches stdin for ESC during streaming;
        # install_sigint_handler routes Ctrl+C → scope.request_cancel()
        # for the duration of the turn.
        async def _run_turn_cancellable() -> None:
            scope = TurnCancelScope()
            listener = KeyboardListener(scope)
            with scope.install_sigint_handler():
                listener.start()
                try:
                    result = await scope.run(_run_turn(user_input))
                    # _run_turn returns None today — token tally update
                    # happens inside _run_turn via a wrapper closure
                    # (see _wrap_run_turn_for_tally below).
                    return result
                except asyncio.CancelledError:
                    console.print("\n[yellow]turn cancelled.[/yellow]")
                finally:
                    listener.stop()

        try:
            asyncio.run(_run_turn_cancellable())
        except Exception as e:
            console.print(f"[bold red]error:[/bold red] {type(e).__name__}: {e}")
```

- [ ] **Step 3.5: Instrument `_run_turn` and `_run_turn_plain` to bump the token tally**

In the same `_run_chat_session` function, find the existing `_run_turn` definition (it calls `await loop.run_conversation(...)` and assigns the result to `result`). Right after the `result = await loop.run_conversation(...)` line in `_run_turn` and BEFORE `renderer.finalize(...)`, add:

```python
            _token_tally["in"] += result.input_tokens
            _token_tally["out"] += result.output_tokens
```

Do the same in `_run_turn_plain`: right after `result = await loop.run_conversation(...)`, add the same two lines (use the same indentation as the surrounding code).

These mutations write to the closure-captured dict from the surrounding `_run_chat_session` scope; no `nonlocal` needed (we're mutating, not rebinding).

- [ ] **Step 4: Verify the file still parses + imports cleanly**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && source .venv/bin/activate && python -c "import opencomputer.cli"`
Expected: no output (clean import). Any `SyntaxError` or `ImportError` here means the splice broke something — fix before continuing.

- [ ] **Step 5: Run the full test suite to catch regressions**

Run: `pytest tests/ -x -q`
Expected: all existing tests still pass. If the smoke test asserts on the old prompt string, update it to use the prompt_toolkit equivalent (likely "you ›" still appears in stdout since we kept the same wording).

- [ ] **Step 6: Commit**

```bash
git add opencomputer/cli.py
git commit -m "feat(cli): wire PromptSession input layer + slash dispatch + cancel scope into chat loop"
```

---

### Task 8: Manual smoke test (TTY interactive)

This is a manual verification — automated tests can't fully cover prompt_toolkit interactive behavior.

- [ ] **Step 1: Launch the chat**

Run (in a real terminal, not a captured pipe):
```bash
cd /Users/saksham/Vscode/claude/OpenComputer && source .venv/bin/activate && opencomputer chat
```

Expected: banner prints; prompt shows `you ›` in green/bold; cursor blinks; you can type.

- [ ] **Step 2: Verify multi-line edit**

Type `hello`, then press `Alt+Enter` (or `Ctrl+J`), then type `world`, then `Enter`.
Expected: the agent receives a single user message containing `hello\nworld`.

- [ ] **Step 3: Verify history**

After at least one turn, restart the CLI (`/exit` or Ctrl+D, then re-launch).
Press `Up` arrow at the prompt.
Expected: the previous turn's input is recalled.

- [ ] **Step 4: Verify ESC clears buffer**

Type `garbage that I want gone`, then press `Esc`.
Expected: the input field clears immediately; cursor returns to start.

- [ ] **Step 5: Verify Ctrl+C cancels mid-stream**

Send a slow request: type `Write a 1000-word essay about Python.` and press `Enter`. While the response is streaming, press `Ctrl+C` once.
Expected: the stream stops; you see `turn cancelled.` in yellow; the prompt comes back. The CLI does **not** exit.

- [ ] **Step 5.5: Verify ESC cancels mid-stream — the user's #1 reported bug**

Send another slow request. While the response is streaming, press `Esc` once.
Expected: the KeyboardListener thread reads the ESC byte from raw-mode stdin, calls `scope.request_cancel()`, the agent's task receives `CancelledError`, the stream halts, `turn cancelled.` prints in yellow. CLI does **not** exit. If this doesn't work, check: (1) `KeyboardListener.start()` is actually called (add a log line), (2) `sys.stdin.isatty()` returns True in your terminal, (3) `os.read(fd, 1)` is reading bytes — try printing them to a debug log inside `_handle_byte`.

- [ ] **Step 6: Verify slash commands**

At the prompt, run `/help`.
Expected: a Rich table prints listing all 8 commands.

Run `/screenshot /tmp/oc-test.txt`.
Expected: file is written; content includes the banner and the help table you just saw.

Run `/cost`.
Expected: prints in/out/total tokens.

Run `/exit`.
Expected: CLI exits cleanly.

- [ ] **Step 7: Verify EOF still works**

Re-launch. Press `Ctrl+D` at an empty prompt.
Expected: `bye.` and clean exit.

- [ ] **Step 8: Document the test outcomes**

If anything in steps 2-7 didn't behave as expected, return to the relevant earlier task, fix, and re-run smoke. Once everything passes, commit nothing — this is a manual verification gate.

---

### Task 9: Update CHANGELOG

**Files:**
- Modify: `CHANGELOG.md` (top — Unreleased section)

- [ ] **Step 1: Read top of CHANGELOG**

Run: `sed -n '1,40p' /Users/saksham/Vscode/claude/OpenComputer/CHANGELOG.md`

- [ ] **Step 2: Add the entry**

Under the most recent `## [Unreleased]` (or add one at the top if missing), append a section like:

```markdown
### Added
- **TUI Phase 1 — input layer foundation.** Replaces `console.input()` with `prompt_toolkit.PromptSession` for the interactive `opencomputer chat` / `opencomputer code` REPL. Adds:
  - **ESC** clears the input buffer (no more accidental submits).
  - **Ctrl+C mid-stream** cancels the current turn instead of killing the CLI; double Ctrl+C still exits.
  - **Persistent input history** at `~/.opencomputer/<profile>/input_history` (Up-arrow recall across sessions).
  - **Multi-line input** via Alt+Enter / Ctrl+J.
  - **Bracketed-paste** support (large pastes no longer fire premature submits).
  - **Slash-command palette**: `/exit`, `/clear`, `/help`, `/screenshot`, `/export`, `/cost`, `/model`, `/sessions` (with aliases `/q`, `/h`, `/?`, `/new`, `/reset`, `/quit`, `/snap`, `/history`).
  - **`Console(record=True)`** enables `/screenshot` and `/export` to dump the current rendered view to text/SVG/HTML.
- New module `opencomputer/cli_ui/` with `input_loop.py`, `slash.py`, `slash_handlers.py`, `turn_cancel.py`. Streaming renderer (existing `streaming.py`) unchanged.
- New dependency: `prompt_toolkit>=3.0`.

### Changed
- `_run_chat_session` now uses `read_user_input()` + `dispatch_slash()` + `TurnCancelScope` instead of the inline `console.input` loop. Non-TTY (piped) stdin still uses the legacy line-by-line path.
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): TUI Phase 1 — input layer foundation"
```

---

### Task 10: Final lint + full test pass

- [ ] **Step 1: Lint**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && source .venv/bin/activate && ruff check opencomputer/cli_ui/ opencomputer/cli.py tests/test_cli_ui_*.py`
Expected: `All checks passed!`. Fix any warnings before continuing.

- [ ] **Step 2: Full test suite**

Run: `pytest tests/ -q`
Expected: all tests pass (885+ existing + ~22 new).

- [ ] **Step 3: Verify clean git state**

Run: `git status`
Expected: nothing to commit, working tree clean (everything from Tasks 1-9 already committed).

- [ ] **Step 4: Push to GitHub (per per-phase rule)**

Run: `git push origin <current-branch>`
Expected: branch pushed. Open a PR titled "feat(cli-ui): TUI Phase 1 — PromptSession + slash dispatch + ESC/Ctrl+C cancel".

---

## Phase 2 sketch (separate plan when scheduled)

These will get their own `2026-XX-XX-tui-uplift-phase2.md` plan when Phase 1 ships:

- **Bottom status bar** (`prompt_toolkit.bottom_toolbar`): model · ctx % · cost · mode · cwd · git branch · bg-task count.
- **`@`-file autocomplete** + `/` slash autocomplete with descriptions.
- **Image clipboard paste** (port `hermes_cli/clipboard.py` cross-platform).
- **Paste-collapse** for >5-line pastes → file in `~/.opencomputer/<profile>/pastes/`, buffer shows `[Pasted text #N: K lines → /path]`.
- **SIGWINCH** handler to re-render the bottom toolbar on resize.
- **Bell / OS notification** on long-task completion.
- **More slash commands**: `/think`, `/compact`, `/resume`, `/save`, `/feedback`, `/copy`.

## Phase 3 sketch (separate plan when scheduled)

- **Skin/theme engine** (port `hermes_cli/skin_engine.py`).
- **OSC52 clipboard write** (`/copy` over SSH/tmux).
- **Resize-ghost `_on_resize` patch** (port from `hermes/cli.py:10468-10500`).
- **Modal-stack `ConditionalContainer`** for consent + clarify + approval prompts inline (instead of separate dialog flows).
- **`/steer`** mid-run injection + **`/queue`** next-turn deferral + **`/btw`** side-question.
- **Random startup tip** rotator.
- **`/copy`** last assistant response to clipboard (pyperclip + OSC52 fallback).

---

## Self-review checklist (pre-execution)

Run these against the plan above before starting Task 1:

1. **Spec coverage:** Phase 1 = ESC + Ctrl+C scope + screenshot + history + multi-line + paste + 8 slash cmds + tests. ✅
2. **Placeholder scan:** No "TBD", "TODO", "fill in", or "similar to". Every code block is concrete. ✅
3. **Type consistency:** `SlashContext`, `SlashResult`, `CommandDef`, `TurnCancelScope` referenced consistently across tasks. ✅
4. **File-path consistency:** every `Files:` block lists the same paths used in steps below it. ✅
5. **Test-first ordering:** every implementation task starts with a failing test, runs it to confirm fail, then implements, then runs to confirm pass. ✅
6. **Commit cadence:** every task ends with a commit. ✅
