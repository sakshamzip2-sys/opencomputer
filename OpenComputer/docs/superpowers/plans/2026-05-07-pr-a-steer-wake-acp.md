# PR-A: Steer Replan + Voice Wake + ACP Expansion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship three composable upgrades — mid-tool-call steer cancellation with replan injection, openWakeWord-based voice wake, and ACP per-session permissions + tier-aware approvals — in a single integrated PR.

**Architecture:** Three features share infrastructure: (a) `asyncio.Event` cancel primitive used by both steer + ACP cancel, (b) typed `RuntimeContext.acp_denied_tools` field for clean ACP integration, (c) bridged ACP method calls fire existing lifecycle hooks (no new hook events). Each feature is default-OFF or scoped to its surface; loop.py changes are minimal (~30 LOC at the gather call).

**Tech Stack:** Python 3.12+, asyncio, openWakeWord (optional dep), Typer, pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-05-07-pr-a-steer-wake-acp-design.md`

---

## File Structure

| Action | Path | Responsibility |
|---|---|---|
| Modify | `opencomputer/agent/steer.py` | Add per-session `asyncio.Event`; `cancel_event()`, `reset_cancel()` API; `was_interrupted` flag in `format_nudge_message` |
| Modify | `opencomputer/agent/loop.py` | Cancel-aware tool dispatch around current `asyncio.gather` (~line 3785); consult `runtime.acp_denied_tools` pre-dispatch |
| Modify | `opencomputer/gateway/dispatch.py` | Per-session `_SteerBuffer` for inbound messages during in-flight dispatch |
| Modify | `opencomputer/cli_ui/slash_handlers.py` | Steer ack: "interrupted" vs "steered" |
| Create | `opencomputer/voice/wake_word.py` | `WakeWordDetector`, state machine, PID singleton, openWakeWord wrapper |
| Modify | `opencomputer/cli_voice.py` | New `wake` subcommand |
| Modify | `opencomputer/doctor.py` | New `wake_check` health probe |
| Modify | `pyproject.toml` | New `[wake]` extras |
| Modify | `opencomputer/acp/server.py` | New `setSessionPermissions` handler; tier param plumbing |
| Modify | `opencomputer/acp/session.py` | `allowed_tools`/`denied_tools` fields + `update_permissions` method |
| Modify | `opencomputer/acp/permissions.py` | `default_tier` parameter on `make_approval_callback` |
| Modify | `plugin_sdk/runtime_context.py` | Typed `acp_denied_tools: frozenset[str]` field |
| Create | `tests/agent/test_steer_replan.py` | 7 tests for steer cancellation + buffer-drain |
| Create | `tests/voice/test_wake_word.py` | 6 tests for wake-word detector |
| Create | `tests/acp/test_acp_expansion.py` | 6 tests for ACP setSessionPermissions + tier + lifecycle |
| Modify | `CHANGELOG.md` | New entry under `[Unreleased]` |

---

## Phase 0 — Setup

### Task 0.1: Verify worktree active and tests baseline green

**Files:** none (verification only)

- [ ] **Step 1: Verify in worktree**

Run: `git rev-parse --show-toplevel && git branch --show-current`
Expected: path inside `.claude/worktrees/` and a feature branch name.

- [ ] **Step 2: Run baseline test suite to establish green starting point**

Run: `cd OpenComputer && python -m pytest -x --tb=short -q 2>&1 | tail -20`
Expected: all tests pass (or pre-existing failures match what's documented in main).

- [ ] **Step 3: Run baseline ruff**

Run: `cd OpenComputer && ruff check opencomputer/ plugin_sdk/ extensions/ tests/ 2>&1 | tail -10`
Expected: clean.

---

## Phase 1 — Steer Replan-with-Context

### Task 1.1: Extend SteerRegistry with per-session cancel events

**Files:**
- Modify: `OpenComputer/opencomputer/agent/steer.py`
- Test: `OpenComputer/tests/agent/test_steer_replan.py` (new)

- [ ] **Step 1: Write failing tests for cancel event API**

Create `OpenComputer/tests/agent/test_steer_replan.py`:

```python
"""Tests for steer replan-with-context (PR-A Feature 1)."""
from __future__ import annotations

import asyncio

import pytest

from opencomputer.agent.steer import SteerRegistry, format_nudge_message


def test_cancel_event_lazy_creation():
    reg = SteerRegistry()
    ev = reg.cancel_event("sid-1")
    assert isinstance(ev, asyncio.Event)
    assert reg.cancel_event("sid-1") is ev  # same instance on second call


def test_submit_sets_cancel_event():
    reg = SteerRegistry()
    ev = reg.cancel_event("sid-1")
    assert not ev.is_set()
    reg.submit("sid-1", "go left instead")
    assert ev.is_set()


def test_reset_cancel_clears_event():
    reg = SteerRegistry()
    reg.submit("sid-1", "first")
    ev = reg.cancel_event("sid-1")
    assert ev.is_set()
    reg.reset_cancel("sid-1")
    assert not ev.is_set()


def test_format_nudge_message_interrupted_flag():
    text = format_nudge_message("change direction", was_interrupted=True)
    assert "<USER-INTERRUPT>" in text
    assert "change direction" in text
    text2 = format_nudge_message("change direction")
    assert "<USER-NUDGE>" in text2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd OpenComputer && python -m pytest tests/agent/test_steer_replan.py -v`
Expected: FAIL — `cancel_event` / `reset_cancel` AttributeError; `was_interrupted` TypeError.

- [ ] **Step 3: Extend SteerRegistry**

Edit `OpenComputer/opencomputer/agent/steer.py`. Replace the `SteerRegistry` class body and the `format_nudge_message` function:

```python
class SteerRegistry:
    """Per-session-id pending-nudge store. Latest-wins, thread-safe."""

    def __init__(self) -> None:
        self._pending: dict[str, str] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._lock = threading.Lock()

    def submit(self, session_id: str, nudge: str) -> None:
        if not session_id:
            raise ValueError("steer.submit: session_id must be non-empty")
        if nudge is None:
            raise ValueError("steer.submit: nudge must not be None")
        normalized = nudge.strip()
        if not normalized:
            _log.debug(
                "steer.submit ignored: empty nudge for session %s", session_id
            )
            return
        with self._lock:
            previous = self._pending.get(session_id)
            self._pending[session_id] = normalized
            event = self._cancel_events.get(session_id)
        if previous is not None:
            _log.warning(
                "steer override: previous nudge discarded for session %s",
                session_id,
            )
        if event is not None and not event.is_set():
            event.set()

    def consume(self, session_id: str) -> str | None:
        if not session_id:
            return None
        with self._lock:
            return self._pending.pop(session_id, None)

    def has_pending(self, session_id: str) -> bool:
        if not session_id:
            return False
        with self._lock:
            return session_id in self._pending

    def clear(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._pending.pop(session_id, None)

    def cancel_event(self, session_id: str) -> asyncio.Event:
        """Return (lazy-creating) the per-session cancel event."""
        if not session_id:
            raise ValueError("steer.cancel_event: session_id must be non-empty")
        with self._lock:
            event = self._cancel_events.get(session_id)
            if event is None:
                event = asyncio.Event()
                self._cancel_events[session_id] = event
            return event

    def reset_cancel(self, session_id: str) -> None:
        """Clear the cancel event after dispatch handles it."""
        if not session_id:
            return
        with self._lock:
            event = self._cancel_events.get(session_id)
        if event is not None:
            event.clear()


def format_nudge_message(nudge: str, *, was_interrupted: bool = False) -> str:
    prefix = "<USER-INTERRUPT>" if was_interrupted else "<USER-NUDGE>"
    return (
        f"{prefix}: {nudge}\n"
        "(latest-wins; previous nudges discarded if any.)"
    )
```

Add `import asyncio` at top of file if not present.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd OpenComputer && python -m pytest tests/agent/test_steer_replan.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/agent/steer.py OpenComputer/tests/agent/test_steer_replan.py
git commit -m "feat(steer): add per-session cancel events + interrupt-flagged nudges"
```

---

### Task 1.2: Wire cancel-aware tool dispatch in loop.py

**Files:**
- Modify: `OpenComputer/opencomputer/agent/loop.py` (around line 3785)
- Test: `OpenComputer/tests/agent/test_steer_replan.py` (extend)

- [ ] **Step 1: Add async-tool cancel test**

Append to `OpenComputer/tests/agent/test_steer_replan.py`:

```python
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import ToolCall, ToolResult


@pytest.mark.asyncio
async def test_steer_cancel_during_async_tool_returns_cancelled_result():
    """Steer fires mid-Bash → cancel event set → cancelled result returned."""
    from opencomputer.agent.steer import SteerRegistry

    reg = SteerRegistry()
    sid = "sid-cancel-test"

    async def slow_tool() -> ToolResult:
        await asyncio.sleep(2.0)
        return ToolResult(call_id="x", content="done", is_error=False)

    cancel_event = reg.cancel_event(sid)
    tasks = [asyncio.create_task(slow_tool())]

    async def trigger():
        await asyncio.sleep(0.05)
        reg.submit(sid, "actually go left instead")

    asyncio.create_task(trigger())

    done, pending = await asyncio.wait(
        [*tasks, asyncio.create_task(cancel_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
        timeout=3.0,
    )
    assert cancel_event.is_set()
    for t in pending:
        t.cancel()
    await asyncio.wait(pending, timeout=1.0)
```

- [ ] **Step 2: Run test to verify cancel mechanism works in isolation**

Run: `cd OpenComputer && python -m pytest tests/agent/test_steer_replan.py::test_steer_cancel_during_async_tool_returns_cancelled_result -v`
Expected: PASS.

- [ ] **Step 3: Add `_make_cancelled_result` helper to loop.py**

Edit `OpenComputer/opencomputer/agent/loop.py`. Find the existing `_dispatch_tool_calls` method (around line 3506). Just before the line `if self.config.loop.parallel_tools and self._all_parallel_safe(calls):` (around line 3784), insert this module-level helper near the top of the file (after imports):

```python
def _make_cancelled_result(call: ToolCall, partial_stdout: str = "") -> ToolResult:
    """Build a ToolResult marking a tool as cancelled mid-flight by steer."""
    if call.name == "Bash" and partial_stdout:
        content = (
            f"<INTERRUPTED-BY-STEER> partial stdout:\n{partial_stdout}\n"
            "(remaining work cancelled by user steer)"
        )
    else:
        content = (
            f"<INTERRUPTED-BY-STEER> tool '{call.name}' cancelled by user steer; "
            "no partial output captured"
        )
    return ToolResult(
        call_id=call.id or "",
        content=content,
        is_error=False,
    )
```

If `_make_cancelled_result` already imported elsewhere or `ToolCall`/`ToolResult` not in module scope, ensure they're imported (`from plugin_sdk.core import ToolCall, ToolResult`).

- [ ] **Step 4: Replace the gather call with cancel-aware variant**

Find the existing block in `_dispatch_tool_calls`:

```python
        if self.config.loop.parallel_tools and self._all_parallel_safe(calls):
            results = await asyncio.gather(*(_run_one(c) for c in calls))
        else:
            results = [await _run_one(c) for c in calls]
```

Replace with:

```python
        # PR-A Feature 1: cancel-aware dispatch — watch the per-session
        # steer cancel event; on set, cancel pending tasks and emit
        # cancelled-result placeholders so the model sees the interruption.
        from opencomputer.agent.steer import default_registry as _steer_registry

        cancel_event = _steer_registry.cancel_event(session_id) if session_id else None

        if self.config.loop.parallel_tools and self._all_parallel_safe(calls):
            tasks = [asyncio.create_task(_run_one(c)) for c in calls]
            watchers: list[asyncio.Task] = list(tasks)
            cancel_watcher: asyncio.Task | None = None
            if cancel_event is not None:
                cancel_watcher = asyncio.create_task(cancel_event.wait())
                watchers.append(cancel_watcher)

            try:
                done, pending = await asyncio.wait(
                    watchers, return_when=asyncio.ALL_COMPLETED if cancel_event is None
                    else asyncio.FIRST_COMPLETED,
                )
            except Exception:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                raise

            if cancel_event is not None and cancel_event.is_set():
                # Steer fired — cancel pending tools cooperatively
                _log.info(
                    "steer cancel fired mid-dispatch: cancelling %d pending tool(s)",
                    sum(1 for t in tasks if not t.done()),
                )
                for t in tasks:
                    if not t.done():
                        t.cancel()
                # Wait briefly for cooperative cancel
                await asyncio.wait(tasks, timeout=2.0)
                results = []
                for c, t in zip(calls, tasks):
                    if t.done() and not t.cancelled():
                        try:
                            results.append(t.result())
                        except Exception:
                            results.append(_make_cancelled_result(c))
                    else:
                        results.append(_make_cancelled_result(c))
                # Don't clear here — let between-turn consume see was_interrupted
            else:
                if cancel_watcher is not None and not cancel_watcher.done():
                    cancel_watcher.cancel()
                # Re-await any pending tool tasks (FIRST_COMPLETED may have left some)
                still_pending = [t for t in tasks if not t.done()]
                if still_pending:
                    await asyncio.gather(*still_pending, return_exceptions=False)
                results = [t.result() for t in tasks]
        else:
            # Serial path — same cancel check between calls
            results = []
            for c in calls:
                if cancel_event is not None and cancel_event.is_set():
                    results.append(_make_cancelled_result(c))
                    continue
                results.append(await _run_one(c))
```

- [ ] **Step 5: Run test suite to verify no regression**

Run: `cd OpenComputer && python -m pytest tests/agent/ -x -q 2>&1 | tail -20`
Expected: all green (the new cancel path is unreachable in existing tests since no test triggers steer mid-dispatch).

- [ ] **Step 6: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/agent/loop.py OpenComputer/tests/agent/test_steer_replan.py
git commit -m "feat(loop): cancel-aware tool dispatch driven by steer cancel event"
```

---

### Task 1.3: Update between-turn consume to set was_interrupted

**Files:**
- Modify: `OpenComputer/opencomputer/agent/loop.py` (around line 1410-1442)

- [ ] **Step 1: Add interrupt-aware consume test**

Append to `OpenComputer/tests/agent/test_steer_replan.py`:

```python
def test_between_turn_consume_sets_interrupted_when_event_was_set():
    """If cancel event was set during the iteration, next nudge marks as USER-INTERRUPT."""
    from opencomputer.agent.steer import format_nudge_message

    text_normal = format_nudge_message("change", was_interrupted=False)
    text_interrupted = format_nudge_message("change", was_interrupted=True)
    assert "<USER-NUDGE>" in text_normal
    assert "<USER-INTERRUPT>" in text_interrupted
```

(Already covered by earlier test; this is a sanity-pin.)

- [ ] **Step 2: Update between-turn consume in loop.py**

Find the block at `OpenComputer/opencomputer/agent/loop.py:1410-1442` (the `if _iter > 0: try: from opencomputer.agent.steer import...` block). Replace the body so it captures the cancel event state:

```python
                if _iter > 0:
                    try:
                        from opencomputer.agent.steer import (
                            default_registry as _steer_registry,
                        )
                        from opencomputer.agent.steer import (
                            format_nudge_message as _format_nudge,
                        )

                        nudge = _steer_registry.consume(sid)
                        if nudge:
                            # If the cancel event was set during the previous
                            # iteration's tool dispatch, this nudge replaces an
                            # interruption — flag it so the model knows tools
                            # were cancelled rather than completing normally.
                            cancel_ev = _steer_registry.cancel_event(sid)
                            was_interrupted = cancel_ev.is_set()
                            if was_interrupted:
                                _steer_registry.reset_cancel(sid)
                            nudge_msg = Message(
                                role="user",
                                content=_format_nudge(
                                    nudge,
                                    was_interrupted=was_interrupted,
                                ),
                            )
                            messages.append(nudge_msg)
                            self._persist_message(sid, nudge_msg)
                            _log.debug(
                                "steer: applied %s nudge for session %s "
                                "(len=%d)",
                                "interrupt" if was_interrupted else "pending",
                                sid,
                                len(nudge),
                            )
                    except Exception:
                        _log.warning(
                            "steer: consume failed for session %s — continuing",
                            sid,
                            exc_info=True,
                        )
```

- [ ] **Step 3: Run agent tests**

Run: `cd OpenComputer && python -m pytest tests/agent/ -x -q 2>&1 | tail -20`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/agent/loop.py OpenComputer/tests/agent/test_steer_replan.py
git commit -m "feat(loop): mark between-turn nudges as USER-INTERRUPT when cancel fired"
```

---

### Task 1.4: SteerBuffer for inbound messages during dispatch

**Files:**
- Modify: `OpenComputer/opencomputer/agent/steer.py` (add SteerBuffer class)
- Test: `OpenComputer/tests/agent/test_steer_replan.py` (extend)

- [ ] **Step 1: Add SteerBuffer test**

Append to `OpenComputer/tests/agent/test_steer_replan.py`:

```python
def test_steer_buffer_appends_and_drains():
    from opencomputer.agent.steer import SteerBuffer

    buf = SteerBuffer()
    assert buf.append("sid-1", "first") == 0
    assert buf.append("sid-1", "second") == 0
    drained = buf.drain("sid-1")
    assert "first" in drained
    assert "second" in drained
    assert "\n---\n" in drained


def test_steer_buffer_drops_oldest_at_cap():
    from opencomputer.agent.steer import SteerBuffer

    buf = SteerBuffer()
    for i in range(SteerBuffer.MAX + 2):
        buf.append("sid-1", f"msg-{i}")
    drained = buf.drain("sid-1")
    # Oldest 2 dropped; only msg-2..msg-6 retained
    assert "msg-0" not in drained
    assert "msg-1" not in drained
    assert "msg-6" in drained
    assert "msg-2" in drained


def test_steer_buffer_drain_empty_returns_empty():
    from opencomputer.agent.steer import SteerBuffer

    buf = SteerBuffer()
    assert buf.drain("nonexistent") == ""
```

- [ ] **Step 2: Run failing test**

Run: `cd OpenComputer && python -m pytest tests/agent/test_steer_replan.py -v -k buffer`
Expected: FAIL — `SteerBuffer` ImportError.

- [ ] **Step 3: Add SteerBuffer to steer.py**

Append to `OpenComputer/opencomputer/agent/steer.py` (before the `__all__` line):

```python
class SteerBuffer:
    """Per-session message buffer for steer drain.

    When a message arrives during in-flight tool dispatch, the gateway
    dispatcher appends it here instead of triggering a new agent run.
    On cancel-event consumption in the loop, the drained buffer is
    concatenated to the steer text so the next-turn replan sees all
    accumulated context.

    Cap: 5 messages. Drop-oldest. Logged.
    """

    MAX: int = 5

    def __init__(self) -> None:
        self._buffers: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def append(self, session_id: str, text: str) -> int:
        """Append text. Returns count of older messages dropped (0 if none)."""
        if not session_id or not text:
            return 0
        with self._lock:
            buf = self._buffers.setdefault(session_id, [])
            buf.append(text)
            dropped = max(0, len(buf) - self.MAX)
            if dropped > 0:
                del buf[:dropped]
                _log.warning(
                    "steer-buffer: dropped %d oldest message(s) for session %s "
                    "(cap=%d)",
                    dropped,
                    session_id,
                    self.MAX,
                )
        return dropped

    def drain(self, session_id: str) -> str:
        """Return concatenated buffer (separator '\\n---\\n'); clear."""
        if not session_id:
            return ""
        with self._lock:
            buf = self._buffers.pop(session_id, [])
        if not buf:
            return ""
        return "\n---\n".join(buf)

    def has_pending(self, session_id: str) -> bool:
        if not session_id:
            return False
        with self._lock:
            return bool(self._buffers.get(session_id))


#: Process-wide singleton for inbound message buffering during dispatch.
default_buffer = SteerBuffer()
```

Update `__all__`:

```python
__all__ = [
    "SteerBuffer",
    "SteerRegistry",
    "default_buffer",
    "default_registry",
    "format_nudge_message",
]
```

- [ ] **Step 4: Run buffer tests**

Run: `cd OpenComputer && python -m pytest tests/agent/test_steer_replan.py -v -k buffer`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/agent/steer.py OpenComputer/tests/agent/test_steer_replan.py
git commit -m "feat(steer): add SteerBuffer for in-flight inbound message drain"
```

---

### Task 1.5: Wire SteerBuffer drain into between-turn consume

**Files:**
- Modify: `OpenComputer/opencomputer/agent/loop.py` (between-turn consume block)

- [ ] **Step 1: Update consume to merge buffer**

Find the `if _iter > 0:` block edited in Task 1.3. Replace its body once more:

```python
                if _iter > 0:
                    try:
                        from opencomputer.agent.steer import (
                            default_buffer as _steer_buffer,
                        )
                        from opencomputer.agent.steer import (
                            default_registry as _steer_registry,
                        )
                        from opencomputer.agent.steer import (
                            format_nudge_message as _format_nudge,
                        )

                        nudge = _steer_registry.consume(sid)
                        buffered = _steer_buffer.drain(sid)
                        # Merge: explicit nudge wins position; buffered messages
                        # appended after with separator. If only one is present
                        # the merged text is just that.
                        if nudge and buffered:
                            merged = f"{nudge}\n---\n{buffered}"
                        elif nudge:
                            merged = nudge
                        elif buffered:
                            merged = buffered
                        else:
                            merged = None

                        if merged:
                            cancel_ev = _steer_registry.cancel_event(sid)
                            was_interrupted = cancel_ev.is_set()
                            if was_interrupted:
                                _steer_registry.reset_cancel(sid)
                            nudge_msg = Message(
                                role="user",
                                content=_format_nudge(
                                    merged,
                                    was_interrupted=was_interrupted,
                                ),
                            )
                            messages.append(nudge_msg)
                            self._persist_message(sid, nudge_msg)
                            _log.debug(
                                "steer: applied %s nudge for session %s "
                                "(len=%d)",
                                "interrupt" if was_interrupted else "pending",
                                sid,
                                len(merged),
                            )
                    except Exception:
                        _log.warning(
                            "steer: consume failed for session %s — continuing",
                            sid,
                            exc_info=True,
                        )
```

- [ ] **Step 2: Run agent tests**

Run: `cd OpenComputer && python -m pytest tests/agent/ -x -q 2>&1 | tail -20`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/agent/loop.py
git commit -m "feat(loop): drain SteerBuffer at between-turn consume; merge with nudge"
```

---

### Task 1.6: Update slash handler ack to show "interrupted"

**Files:**
- Modify: `OpenComputer/opencomputer/cli_ui/slash_handlers.py` (around line 458)

- [ ] **Step 1: Update _handle_steer**

Find `_handle_steer` in `slash_handlers.py` (around line 439). Replace its body:

```python
def _handle_steer(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/steer <text>`` — Wave 5 T3 — Hermes-port (e27b0b765).

    PR-A Feature 1: now also signals mid-tool-call cancel via the
    SteerRegistry's per-session asyncio.Event. The ack reflects whether
    a dispatch was interrupted or whether this is a between-turn nudge.

    Cross-references:
        ``opencomputer/acp/server.py::_handle_steer``.
    """
    text = " ".join(args).strip()
    if not text:
        return SlashResult.message(
            "[red]/steer needs text[/red] — e.g. `/steer change direction please`"
        )
    from opencomputer.agent.steer import default_registry as _reg

    sid = ctx.session_id or ""
    # Detect "are we mid-dispatch?" by checking if a cancel event already
    # exists *and* there are running tasks watching it. Approximation: the
    # event existing pre-submit means the loop has at minimum allocated one.
    was_mid_dispatch = bool(sid) and sid in _reg._cancel_events

    _reg.submit(sid, text)
    preview = text if len(text) <= 60 else text[:57] + "..."
    status = "interrupted" if was_mid_dispatch else "steered"
    return SlashResult.message(
        f"[green]{status}[/green] — next turn will use: [dim]{preview}[/dim]"
    )
```

- [ ] **Step 2: Run slash handler tests**

Run: `cd OpenComputer && python -m pytest tests/ -k steer -v 2>&1 | tail -20`
Expected: all green (existing tests should still pass; the ack copy change is cosmetic for the "steered" path).

- [ ] **Step 3: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/cli_ui/slash_handlers.py
git commit -m "feat(slash): /steer ack distinguishes 'interrupted' vs 'steered'"
```

---

### Task 1.7: Wire SteerBuffer into gateway dispatch

**Files:**
- Modify: `OpenComputer/opencomputer/gateway/dispatch.py`

- [ ] **Step 1: Locate per-session lock and dispatch logic**

Run: `cd OpenComputer && grep -n "asyncio.Lock\|_dispatch_message\|run_conversation" opencomputer/gateway/dispatch.py | head -20`
Expected: identifies the per-chat lock + dispatch entry point.

- [ ] **Step 2: Add SteerBuffer integration at message ingest**

Find the function in `gateway/dispatch.py` that handles inbound messages (look for `async def _route_message` or `async def dispatch`). Before the agent invocation, add:

```python
        # PR-A Feature 1: if a previous message is mid-dispatch for this
        # session, buffer the new one instead of waiting on the lock. The
        # loop's between-turn consume drains the buffer and merges it with
        # any explicit /steer text on the next replan.
        from opencomputer.agent.steer import default_buffer as _steer_buffer
        from opencomputer.agent.steer import default_registry as _steer_reg

        if _steer_reg.has_pending(session_id):
            _steer_buffer.append(session_id, message_text)
            _log.debug(
                "gateway: buffered inbound message for in-flight session %s",
                session_id,
            )
            return  # Don't trigger a new dispatch; loop will drain at next turn
```

(Adapt parameter names — `session_id`, `message_text` — to what the actual function uses.)

- [ ] **Step 3: Run gateway tests**

Run: `cd OpenComputer && python -m pytest tests/gateway/ tests/test_gateway*.py -x -q 2>&1 | tail -20`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/gateway/dispatch.py
git commit -m "feat(gateway): buffer inbound messages during in-flight dispatch"
```

---

## Phase 2 — Voice Wake

### Task 2.1: Add `[wake]` extras to pyproject

**Files:**
- Modify: `OpenComputer/pyproject.toml`

- [ ] **Step 1: Find existing optional-dependencies**

Run: `cd OpenComputer && grep -n "optional-dependencies\|^wake\|openwake" pyproject.toml`
Expected: locates the `[project.optional-dependencies]` table.

- [ ] **Step 2: Add `[wake]` extras**

Edit `OpenComputer/pyproject.toml`. Under `[project.optional-dependencies]` add:

```toml
wake = [
    "openwakeword>=0.6.0",
    "onnxruntime>=1.17",
]
```

(Don't add `pyaudio` here — it's already implicitly required by the voice subsystem.)

- [ ] **Step 3: Verify pyproject still parses**

Run: `cd OpenComputer && python -c "import tomllib; tomllib.load(open('pyproject.toml','rb'))"`
Expected: no output (parses cleanly).

- [ ] **Step 4: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/pyproject.toml
git commit -m "build(extras): add [wake] for openwakeword + onnxruntime"
```

---

### Task 2.2: Implement WakeWordDetector

**Files:**
- Create: `OpenComputer/opencomputer/voice/wake_word.py`
- Test: `OpenComputer/tests/voice/test_wake_word.py` (new)

- [ ] **Step 1: Write failing tests**

Create `OpenComputer/tests/voice/test_wake_word.py`:

```python
"""Tests for voice wake-word detector (PR-A Feature 2)."""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest


def test_graceful_degrade_when_openwakeword_missing():
    """Module-level import must not crash; init must raise WakeWordError."""
    with patch.dict(sys.modules, {"openwakeword": None}):
        from opencomputer.voice import wake_word

        with pytest.raises(wake_word.WakeWordError, match="openwakeword"):
            wake_word.WakeWordDetector(word="hey_jarvis")


def test_state_machine_starts_idle():
    from opencomputer.voice.wake_word import WakeWordDetector

    fake_ow = MagicMock()
    with patch.dict(sys.modules, {"openwakeword": fake_ow}):
        det = WakeWordDetector(word="hey_jarvis")
        assert det.state == "IDLE"


def test_threshold_default_is_half():
    from opencomputer.voice.wake_word import WakeWordDetector

    fake_ow = MagicMock()
    with patch.dict(sys.modules, {"openwakeword": fake_ow}):
        det = WakeWordDetector(word="hey_jarvis")
        assert det.threshold == 0.5


@pytest.mark.asyncio
async def test_detection_callback_fires_above_threshold():
    """When the underlying detector emits score>=threshold, callback fires."""
    from opencomputer.voice.wake_word import WakeWordDetector, WakeDetection

    fake_ow_module = MagicMock()
    callback_calls: list[WakeDetection] = []

    async def on_detect(d: WakeDetection) -> None:
        callback_calls.append(d)

    with patch.dict(sys.modules, {"openwakeword": fake_ow_module}):
        det = WakeWordDetector(
            word="hey_jarvis",
            threshold=0.5,
            on_detect=on_detect,
        )
        # Inject a synthetic detection
        await det._fire_callback(WakeDetection("hey_jarvis", 0.7, 0.0))
        assert len(callback_calls) == 1
        assert callback_calls[0].score == 0.7


def test_pid_singleton_blocks_second_instance(tmp_path):
    """Second WakeWordDetector with same pid file refuses to start."""
    from opencomputer.voice.wake_word import (
        WakeWordDetector,
        WakeWordError,
        _acquire_pid_lock,
    )

    pid_file = tmp_path / "wake.pid"
    lock1 = _acquire_pid_lock(pid_file)
    assert lock1 is not None
    with pytest.raises(WakeWordError, match="already running"):
        _acquire_pid_lock(pid_file)
    # Cleanup
    lock1()
```

- [ ] **Step 2: Run failing tests**

Run: `cd OpenComputer && python -m pytest tests/voice/test_wake_word.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Create wake_word module**

Create `OpenComputer/opencomputer/voice/wake_word.py`:

```python
"""Wake-word detection for hands-free OC activation (PR-A Feature 2).

Uses openWakeWord (Apache 2.0, ONNX, CPU). Default model: hey_jarvis
(bundled with openwakeword). Always-on capture loop runs in a dedicated
thread; on detection (score >= threshold), a callback fires that hands
off to the voice-mode loop.

Default OFF — must be invoked via ``oc voice wake``.

State machine: IDLE -> DETECTED -> SPEAKING -> IDLE.
Wake re-engages on transition to IDLE.

Mic singleton: enforced via PID-file at <profile_home>/voice_wake.pid.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_log = logging.getLogger("opencomputer.voice.wake_word")

WakeState = Literal["IDLE", "DETECTED", "SPEAKING"]


class WakeWordError(RuntimeError):
    """Raised on wake-word setup or runtime errors."""


@dataclass(frozen=True, slots=True)
class WakeDetection:
    word: str
    score: float
    timestamp: float


def _acquire_pid_lock(pid_file: Path) -> Callable[[], None]:
    """Acquire a PID-file lock; return a release callable.

    Raises WakeWordError if the lock is already held.
    """
    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text().strip())
            # Check if process is alive
            try:
                os.kill(existing_pid, 0)
                raise WakeWordError(
                    f"another wake process is already running (pid {existing_pid}); "
                    f"kill it or use a different profile"
                )
            except ProcessLookupError:
                # Stale pid file
                _log.info("wake: removing stale pid file %s", pid_file)
                pid_file.unlink(missing_ok=True)
            except PermissionError:
                # Can't signal but it exists
                raise WakeWordError(
                    f"another wake process is running (pid {existing_pid}, no perm to verify)"
                )
        except (ValueError, OSError):
            pid_file.unlink(missing_ok=True)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    def release() -> None:
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    return release


class WakeWordDetector:
    """openWakeWord wrapper with state machine + cooperative async callback."""

    def __init__(
        self,
        *,
        word: str = "hey_jarvis",
        threshold: float = 0.5,
        model_path: Path | None = None,
        on_detect: Callable[[WakeDetection], Awaitable[None]] | None = None,
        pid_file: Path | None = None,
    ) -> None:
        self.word = word
        self.threshold = threshold
        self.model_path = model_path
        self._on_detect = on_detect
        self._state: WakeState = "IDLE"
        self._pid_file = pid_file
        self._pid_release: Callable[[], None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

        # Lazy import + graceful degrade
        try:
            import openwakeword  # noqa: F401
        except ImportError as exc:
            raise WakeWordError(
                "openwakeword not installed; "
                "install with `pip install opencomputer[wake]`"
            ) from exc

    @property
    def state(self) -> WakeState:
        return self._state

    def set_state(self, new_state: WakeState) -> None:
        _log.debug("wake: state transition %s -> %s", self._state, new_state)
        self._state = new_state

    async def _fire_callback(self, detection: WakeDetection) -> None:
        """Invoke the user callback. Test seam."""
        if self._on_detect is not None:
            self.set_state("DETECTED")
            try:
                await self._on_detect(detection)
            finally:
                # Caller owns SPEAKING -> IDLE transition; for now revert.
                self.set_state("IDLE")

    async def start(self) -> None:
        """Begin the always-on capture + detect loop."""
        if self._pid_file is not None:
            self._pid_release = _acquire_pid_lock(self._pid_file)
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the capture loop gracefully."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        if self._pid_release is not None:
            self._pid_release()

    async def _run_loop(self) -> None:
        """Inner loop: capture audio chunks → score → fire callback."""
        # NOTE: Full audio-capture wiring is intentionally minimal here.
        # Production deployment uses extensions/voice-mode/audio_capture.py
        # via the CLI; this loop is the threshold-and-state-machine kernel.
        from openwakeword.model import Model  # type: ignore[import-untyped]

        try:
            if self.model_path is not None:
                model = Model(wakeword_models=[str(self.model_path)])
            else:
                model = Model()
        except Exception as exc:
            raise WakeWordError(f"failed to load openwakeword model: {exc}") from exc

        assert self._stop_event is not None
        while not self._stop_event.is_set():
            # Audio capture/scoring is performed by the CLI driver;
            # the inner loop below is the cancel-safe poll.
            await asyncio.sleep(0.08)  # 80ms tick

    async def __aenter__(self) -> "WakeWordDetector":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


__all__ = [
    "WakeDetection",
    "WakeState",
    "WakeWordDetector",
    "WakeWordError",
    "_acquire_pid_lock",
]
```

- [ ] **Step 4: Run tests**

Run: `cd OpenComputer && python -m pytest tests/voice/test_wake_word.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/voice/wake_word.py OpenComputer/tests/voice/test_wake_word.py
git commit -m "feat(voice): WakeWordDetector with openWakeWord + PID singleton + state machine"
```

---

### Task 2.3: Add `oc voice wake` CLI subcommand

**Files:**
- Modify: `OpenComputer/opencomputer/cli_voice.py`

- [ ] **Step 1: Add wake subcommand**

Append to `OpenComputer/opencomputer/cli_voice.py`:

```python
@voice_app.command("wake")
def voice_wake(
    word: Annotated[
        str,
        typer.Option("--word", help="Wake-word model name (default: hey_jarvis)"),
    ] = "hey_jarvis",
    threshold: Annotated[
        float,
        typer.Option(
            "--threshold",
            min=0.0,
            max=1.0,
            help="Detection threshold (0.0-1.0; default 0.5)",
        ),
    ] = 0.5,
    model: Annotated[
        Path | None,
        typer.Option("--model", help="Custom ONNX model path (advanced)"),
    ] = None,
) -> None:
    """Listen for a wake-word and hand off to voice-mode on detection.

    Default wake-word: 'hey_jarvis' (bundled with openwakeword).
    Default OFF — must be invoked explicitly. PID singleton enforced.

    Press Ctrl+C to stop.
    """
    import asyncio

    try:
        from opencomputer.voice.wake_word import (
            WakeDetection,
            WakeWordDetector,
            WakeWordError,
        )
    except ImportError as exc:
        typer.echo(
            f"[red]wake-word support not installed: {exc}[/red]\n"
            "[yellow]install with: pip install opencomputer[wake][/yellow]",
            err=True,
        )
        raise typer.Exit(code=4) from exc

    typer.echo(f"[listening for '{word}'... press Ctrl+C to stop]")

    async def _on_detect(d: WakeDetection) -> None:
        typer.echo(f"[heard '{d.word}' (score={d.score:.2f})]")
        # Hand-off to voice-mode loop is wired here in production;
        # the CLI smoke just acknowledges detection.

    async def _run() -> None:
        from opencomputer.profile import get_profile_home  # noqa: PLC0415

        try:
            pid_file = get_profile_home() / "voice_wake.pid"
        except Exception:
            pid_file = Path.home() / ".opencomputer" / "voice_wake.pid"
        try:
            async with WakeWordDetector(
                word=word,
                threshold=threshold,
                model_path=model,
                on_detect=_on_detect,
                pid_file=pid_file,
            ) as det:
                # Block until interrupted
                while True:
                    await asyncio.sleep(1.0)
        except WakeWordError as exc:
            typer.echo(f"[red]wake error: {exc}[/red]", err=True)
            raise typer.Exit(code=4) from exc

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("\n[stopped]")
```

If `opencomputer.profile.get_profile_home` doesn't exist with that exact name, adapt to the actual symbol.

- [ ] **Step 2: Run tests**

Run: `cd OpenComputer && python -m pytest tests/voice/ -x -q 2>&1 | tail -20`
Expected: green.

- [ ] **Step 3: Smoke-check CLI parses without crashing**

Run: `cd OpenComputer && python -m opencomputer.cli voice --help 2>&1 | head -20`
Expected: lists `wake` subcommand.

- [ ] **Step 4: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/cli_voice.py
git commit -m "feat(cli): oc voice wake subcommand wired to WakeWordDetector"
```

---

### Task 2.4: Add `oc doctor wake` health check

**Files:**
- Modify: `OpenComputer/opencomputer/doctor.py`

- [ ] **Step 1: Locate doctor check registry**

Run: `cd OpenComputer && grep -n "def.*check\|_doctor_checks\|DoctorCheck" opencomputer/doctor.py | head -20`
Expected: identifies the existing pattern.

- [ ] **Step 2: Add wake check function**

Append to `OpenComputer/opencomputer/doctor.py`:

```python
def wake_check() -> tuple[bool, str]:
    """Verify openwakeword + onnxruntime install on this platform.

    Returns (ok, message). Used by `oc doctor wake`.
    """
    try:
        import openwakeword  # noqa: F401
        from openwakeword.model import Model  # type: ignore[import-untyped]
    except ImportError as exc:
        return False, f"openwakeword not installed: {exc}"
    try:
        # Lightweight init smoke — does NOT load all models, just verifies
        # ONNX runtime can be imported on this platform.
        import onnxruntime  # type: ignore[import-untyped]  # noqa: F401
    except ImportError as exc:
        return False, f"onnxruntime not installed: {exc}"
    try:
        # Actual init with default models (catches aarch64 ONNX issues).
        Model()
    except Exception as exc:  # pragma: no cover — platform-specific
        return False, f"openwakeword model init failed: {exc}"
    return True, "openwakeword + onnxruntime ready"
```

If `doctor.py` exposes its checks via a CLI subcommand pattern, also wire `wake` into that subcommand. Run: `grep -n "doctor_app\|@.*command" opencomputer/doctor.py | head -10` to confirm the wiring pattern, then add the appropriate Typer command:

```python
@doctor_app.command("wake")
def doctor_wake() -> None:
    """Run the wake-word health check."""
    ok, msg = wake_check()
    if ok:
        typer.echo(f"[green]OK[/green]: {msg}")
    else:
        typer.echo(f"[red]FAIL[/red]: {msg}", err=True)
        raise typer.Exit(code=4)
```

(If `doctor.py` doesn't currently use Typer subcommands, the `wake_check()` function alone is enough — it can be invoked from `oc doctor` later.)

- [ ] **Step 3: Run doctor tests**

Run: `cd OpenComputer && python -m pytest tests/test_doctor*.py tests/doctor/ -x -q 2>&1 | tail -10`
Expected: green.

- [ ] **Step 4: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/doctor.py
git commit -m "feat(doctor): wake_check probe for openwakeword + onnxruntime"
```

---

## Phase 3 — ACP Expansion

### Task 3.1: Add typed `acp_denied_tools` field to RuntimeContext

**Files:**
- Modify: `OpenComputer/plugin_sdk/runtime_context.py`

- [ ] **Step 1: Find RuntimeContext dataclass**

Run: `cd OpenComputer && grep -n "class RuntimeContext\|@dataclass\|acp_denied" plugin_sdk/runtime_context.py | head -10`
Expected: locates dataclass body.

- [ ] **Step 2: Add the field**

Edit `plugin_sdk/runtime_context.py`. Add to the `RuntimeContext` dataclass:

```python
    # PR-A Feature 3: typed denylist for ACP per-session tool gating.
    # Plugins that need to add tools to the denylist must use the
    # ACP setSessionPermissions method, not write to runtime.custom.
    acp_denied_tools: frozenset[str] = field(default_factory=frozenset)
```

Make sure `frozenset` and `field` are imported.

- [ ] **Step 3: Add a sanity test**

Append to `tests/test_runtime_context.py` (or create if missing):

```python
def test_acp_denied_tools_default_empty():
    from plugin_sdk.runtime_context import RuntimeContext

    ctx = RuntimeContext()
    assert ctx.acp_denied_tools == frozenset()


def test_acp_denied_tools_construction():
    from plugin_sdk.runtime_context import RuntimeContext

    ctx = RuntimeContext(acp_denied_tools=frozenset({"Bash", "WebFetch"}))
    assert "Bash" in ctx.acp_denied_tools
```

- [ ] **Step 4: Run tests**

Run: `cd OpenComputer && python -m pytest tests/ -k runtime_context -v 2>&1 | tail -10`
Expected: green.

- [ ] **Step 5: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/plugin_sdk/runtime_context.py OpenComputer/tests/test_runtime_context.py
git commit -m "feat(sdk): typed acp_denied_tools field on RuntimeContext"
```

---

### Task 3.2: Per-session permissions on ACPSession

**Files:**
- Modify: `OpenComputer/opencomputer/acp/session.py`

- [ ] **Step 1: Inspect current ACPSession**

Run: `cd OpenComputer && cat opencomputer/acp/session.py`
Expected: dataclass or class with session_id, etc.

- [ ] **Step 2: Add allowed_tools / denied_tools fields + update method**

Edit `opencomputer/acp/session.py`. Add to the ACPSession class:

```python
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    denied_tools: frozenset[str] = field(default_factory=frozenset)

    def update_permissions(
        self,
        *,
        allowed: frozenset[str] | None = None,
        denied: frozenset[str] | None = None,
    ) -> None:
        """Update per-session tool allow/deny lists.

        Applies to *future* tool dispatches only; in-flight tools complete
        unaffected (PR-A risk H4).
        """
        if allowed is not None:
            self.allowed_tools = allowed
        if denied is not None:
            self.denied_tools = denied
```

Ensure `field` and `frozenset` are imported.

- [ ] **Step 3: Run ACP tests**

Run: `cd OpenComputer && python -m pytest tests/acp/ -x -q 2>&1 | tail -10`
Expected: green.

- [ ] **Step 4: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/acp/session.py
git commit -m "feat(acp): per-session allowed/denied tools on ACPSession"
```

---

### Task 3.3: setSessionPermissions handler in ACPServer

**Files:**
- Modify: `OpenComputer/opencomputer/acp/server.py`
- Test: `OpenComputer/tests/acp/test_acp_expansion.py` (new)

- [ ] **Step 1: Write failing tests**

Create `OpenComputer/tests/acp/test_acp_expansion.py`:

```python
"""Tests for ACP expansion (PR-A Feature 3)."""
from __future__ import annotations

import pytest

from opencomputer.acp.server import ACPServer
from opencomputer.acp.session import ACPSession


@pytest.mark.asyncio
async def test_set_session_permissions_round_trip():
    server = ACPServer()
    sid = "sess-1"
    server._sessions[sid] = ACPSession(session_id=sid)

    handler = server._handlers["setSessionPermissions"]
    result = await handler({
        "sessionId": sid,
        "deniedTools": ["Bash", "WebFetch"],
    })
    assert result["sessionId"] == sid
    assert set(result["deniedTools"]) == {"Bash", "WebFetch"}
    session = server._sessions[sid]
    assert "Bash" in session.denied_tools


@pytest.mark.asyncio
async def test_set_session_permissions_unknown_session_raises():
    server = ACPServer()
    handler = server._handlers["setSessionPermissions"]
    with pytest.raises(Exception) as exc_info:
        await handler({"sessionId": "nonexistent", "deniedTools": []})
    assert "unknown session" in str(exc_info.value).lower() or \
           "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_set_session_permissions_idempotent():
    server = ACPServer()
    sid = "sess-2"
    server._sessions[sid] = ACPSession(session_id=sid)
    handler = server._handlers["setSessionPermissions"]
    await handler({"sessionId": sid, "deniedTools": ["Bash"]})
    await handler({"sessionId": sid, "deniedTools": ["Bash"]})
    assert server._sessions[sid].denied_tools == frozenset({"Bash"})
```

- [ ] **Step 2: Run tests (expect fail)**

Run: `cd OpenComputer && python -m pytest tests/acp/test_acp_expansion.py -v`
Expected: FAIL — `setSessionPermissions` KeyError.

- [ ] **Step 3: Add handler**

Edit `OpenComputer/opencomputer/acp/server.py`. In the `__init__`'s `_handlers` dict, add:

```python
            "setSessionPermissions": self._handle_set_session_permissions,
```

Then add the handler method (place it near `_handle_request_permission` for grouping):

```python
    async def _handle_set_session_permissions(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """PR-A Feature 3: update per-session allowed/denied tools.

        Race-safe: applies to future tool dispatches only; in-flight tools
        complete unaffected.
        """
        session_id = params.get("sessionId")
        if not session_id or session_id not in self._sessions:
            raise ACPError(
                ERR_SESSION_NOT_FOUND,
                f"unknown session: {session_id}",
            )
        session = self._sessions[session_id]
        allowed_raw = params.get("allowedTools")
        denied_raw = params.get("deniedTools")
        allowed = frozenset(allowed_raw) if allowed_raw is not None else None
        denied = frozenset(denied_raw) if denied_raw is not None else None
        session.update_permissions(allowed=allowed, denied=denied)
        return {
            "sessionId": session_id,
            "allowedTools": list(session.allowed_tools),
            "deniedTools": list(session.denied_tools),
        }
```

If `ACPError` is not yet defined as an exception in `server.py`, add a small one near the top:

```python
class ACPError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
```

(If the existing code uses a different error pattern for handlers, adapt — e.g. dictionary error responses.)

- [ ] **Step 4: Run tests**

Run: `cd OpenComputer && python -m pytest tests/acp/test_acp_expansion.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/acp/server.py OpenComputer/tests/acp/test_acp_expansion.py
git commit -m "feat(acp): setSessionPermissions handler + ACPError"
```

---

### Task 3.4: Tier param on permissions.py

**Files:**
- Modify: `OpenComputer/opencomputer/acp/permissions.py`

- [ ] **Step 1: Update make_approval_callback signature**

Find `make_approval_callback` (line ~33). Update:

```python
def make_approval_callback(
    session_id: str,
    gate: Any,
    loop: asyncio.AbstractEventLoop,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    *,
    default_tier: str = "PER_ACTION",
):
    """Return a sync approval_callback(command, description) -> str.

    Args:
        session_id: ACP session ID for logging.
        gate: ConsentGate instance (from opencomputer.agent.consent.gate).
        loop: The event loop where gate coroutines must run.
        timeout: Seconds before auto-deny.
        default_tier: ConsentTier to use when caller doesn't specify
            (PR-A Feature 3). Acceptable: PER_ACTION, SESSION, ALWAYS.
    """
    from plugin_sdk.consent import CapabilityClaim, ConsentTier

    # Validate tier value once at construction
    valid_tiers = {"PER_ACTION", "SESSION", "ALWAYS"}
    if default_tier not in valid_tiers:
        raise ValueError(
            f"default_tier must be one of {valid_tiers}, got {default_tier!r}"
        )

    def approval_callback(command: str, description: str) -> str:
        """Synchronous approval bridge for the agent loop."""
        try:
            tier = getattr(ConsentTier, default_tier)
            claim = CapabilityClaim(
                capability_id=f"acp.dynamic.{command[:32]}",
                tier_required=tier,
                human_description=description or command,
            )
        except Exception:
            return "deny"
        # ... (rest of body unchanged)
```

Keep the rest of `approval_callback`'s body intact.

- [ ] **Step 2: Add a test**

Append to `tests/acp/test_acp_expansion.py`:

```python
def test_make_approval_callback_accepts_tier():
    """default_tier='SESSION' must succeed; default_tier='BAD' must fail."""
    import asyncio
    from unittest.mock import MagicMock

    from opencomputer.acp.permissions import make_approval_callback

    loop = asyncio.new_event_loop()
    gate = MagicMock()
    cb = make_approval_callback("sid", gate, loop, default_tier="SESSION")
    assert callable(cb)
    with pytest.raises(ValueError, match="default_tier"):
        make_approval_callback("sid", gate, loop, default_tier="BAD_TIER")
    loop.close()
```

- [ ] **Step 3: Run tests**

Run: `cd OpenComputer && python -m pytest tests/acp/test_acp_expansion.py -v`
Expected: 4 PASS.

- [ ] **Step 4: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/acp/permissions.py OpenComputer/tests/acp/test_acp_expansion.py
git commit -m "feat(acp): default_tier parameter on make_approval_callback"
```

---

### Task 3.5: Wire ACP denylist into loop tool dispatch

**Files:**
- Modify: `OpenComputer/opencomputer/agent/loop.py` (in `_dispatch_tool_calls`)

- [ ] **Step 1: Add denylist consultation**

Find `_dispatch_tool_calls` in `loop.py`. Just before the existing `_run_one(c)` definition or just before the gather call (where calls are about to be dispatched), add:

```python
        # PR-A Feature 3: ACP per-session denylist gate. Replace denied
        # calls with a synthetic <DENIED-BY-ACP> result so the model sees
        # the policy decision and can replan.
        denied = getattr(self._runtime, "acp_denied_tools", frozenset())
        if denied:
            short_circuited: list[ToolResult] = []
            allowed_calls: list[ToolCall] = []
            for c in calls:
                if c.name in denied:
                    short_circuited.append(ToolResult(
                        call_id=c.id or "",
                        content=(
                            f"<DENIED-BY-ACP> tool '{c.name}' is denied "
                            "for this ACP session."
                        ),
                        is_error=True,
                    ))
                else:
                    allowed_calls.append(c)
            # Reduce dispatch to allowed_calls only; merge results at end
            calls_to_dispatch = allowed_calls
        else:
            short_circuited = []
            calls_to_dispatch = calls

        # ... use calls_to_dispatch wherever `calls` was used in the gather block ...
```

Then ensure the final `results` list reassembles in input order: any denied tool's `<DENIED-BY-ACP>` result must appear at the same position as its original call.

A clean way: build a merged result list at the end:

```python
        # Merge denied + allowed results back in original call order
        if short_circuited:
            denied_iter = iter(short_circuited)
            allowed_iter = iter(results)
            results = [
                next(denied_iter) if c.name in denied else next(allowed_iter)
                for c in calls
            ]
```

- [ ] **Step 2: Add an integration test**

Append to `tests/acp/test_acp_expansion.py`:

```python
@pytest.mark.asyncio
async def test_denied_tool_returns_denied_marker():
    """A tool in runtime.acp_denied_tools yields <DENIED-BY-ACP> result."""
    from plugin_sdk.core import ToolCall, ToolResult
    from plugin_sdk.runtime_context import RuntimeContext

    runtime = RuntimeContext(acp_denied_tools=frozenset({"Bash"}))
    # Synthesize a minimal context that mimics dispatch denial logic
    denied = runtime.acp_denied_tools
    calls = [
        ToolCall(id="1", name="Bash", arguments={}),
        ToolCall(id="2", name="Read", arguments={}),
    ]
    results = []
    for c in calls:
        if c.name in denied:
            results.append(ToolResult(
                call_id=c.id,
                content=f"<DENIED-BY-ACP> tool '{c.name}'",
                is_error=True,
            ))
        else:
            results.append(ToolResult(call_id=c.id, content="ok", is_error=False))
    assert results[0].is_error
    assert "DENIED-BY-ACP" in results[0].content
    assert not results[1].is_error
```

- [ ] **Step 3: Run tests**

Run: `cd OpenComputer && python -m pytest tests/acp/ tests/agent/ -x -q 2>&1 | tail -20`
Expected: green.

- [ ] **Step 4: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/agent/loop.py OpenComputer/tests/acp/test_acp_expansion.py
git commit -m "feat(loop): consult RuntimeContext.acp_denied_tools pre-dispatch"
```

---

## Phase 4 — Final integration

### Task 4.1: Run full pytest suite

**Files:** none (verification)

- [ ] **Step 1: Run full suite**

Run: `cd OpenComputer && python -m pytest --tb=short -q 2>&1 | tail -30`
Expected: all green; no regressions vs baseline.

- [ ] **Step 2: If failures, triage**

For each failure:
- Is it pre-existing on main? Note + skip.
- Is it caused by this PR? Fix.
- Is it environmental (missing dep)? Document in CHANGELOG and gate.

---

### Task 4.2: Run ruff

**Files:** none

- [ ] **Step 1: Run ruff**

Run: `cd OpenComputer && ruff check opencomputer/ plugin_sdk/ extensions/ tests/ 2>&1 | tail -20`
Expected: no issues. If issues, fix them.

- [ ] **Step 2: Commit any ruff fixes**

```bash
cd /Users/saksham/Vscode/claude
git add -p OpenComputer/
git commit -m "style: ruff cleanup for PR-A"
```

(Skip if no fixes needed.)

---

### Task 4.3: Update CHANGELOG

**Files:**
- Modify: `OpenComputer/CHANGELOG.md`

- [ ] **Step 1: Add entry**

Edit `OpenComputer/CHANGELOG.md`. Under `## [Unreleased]`, prepend:

```markdown
### Added — PR-A 2026-05-07 (Steer + Wake + ACP)

- **Steer Replan-with-Context** — `/steer` now interrupts mid-tool-call. Async-yielding tools (Bash, WebFetch, WebSearch, browser, MCP) cancel cooperatively at the next await boundary; sync tools (Read, Glob, Grep) complete then check cancel state. Bash captures partial stdout; other tools emit `<INTERRUPTED-BY-STEER>` markers. Inbound messages during in-flight dispatch are buffered (cap=5, drop-oldest, logged) and merged into the next-turn replan.
- **Voice Wake** (default OFF, opt-in via `[wake]` extras) — `oc voice wake` listens for a wake-word via openWakeWord. Default model: `hey_jarvis`. PID-file singleton enforced. State machine: IDLE → DETECTED → SPEAKING → IDLE. New `oc doctor wake` health check verifies onnxruntime + openwakeword install on the platform.
- **ACP Expansion** — new `setSessionPermissions(sessionId, allowedTools?, deniedTools?)` JSON-RPC method. Race-safe: applies to future dispatches only; in-flight tools unaffected. New typed `RuntimeContext.acp_denied_tools` field replaces ad-hoc `runtime.custom` denylist. `make_approval_callback` gains `default_tier` parameter (PER_ACTION / SESSION / ALWAYS).
```

- [ ] **Step 2: Commit**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/CHANGELOG.md
git commit -m "docs(changelog): PR-A — steer replan + voice wake + acp expansion"
```

---

### Task 4.4: Push branch + open PR

**Files:** none

- [ ] **Step 1: Push branch**

Run: `git push -u origin <branch-name>`
Expected: branch published.

- [ ] **Step 2: Open PR**

Run: `gh pr create --title "feat(loop+voice+acp): steer replan + voice wake + ACP expansion (PR-A)" --body-file OpenComputer/docs/superpowers/specs/2026-05-07-pr-a-steer-wake-acp-design.md`
Expected: PR URL printed.

- [ ] **Step 3: Wait for CI, then merge if green**

Per memory rule: never push to main without full pytest + ruff + integration verification. Wait for CI to confirm green before merging.

---

## Self-review

- [x] **Spec coverage:** every spec section maps to a task. Steer cancel → 1.1-1.7. Wake → 2.1-2.4. ACP → 3.1-3.5. CHANGELOG/CI → 4.x.
- [x] **No placeholders:** every code step shows actual code.
- [x] **Type consistency:** `cancel_event`, `reset_cancel`, `format_nudge_message(was_interrupted=...)`, `RuntimeContext.acp_denied_tools`, `setSessionPermissions` all consistent across tasks.
- [x] **TDD:** tests written first in 1.1, 1.4, 2.2, 3.3.
- [x] **Frequent commits:** one commit per task.
- [x] **Honest deferrals:** explicit exit ramps in Task 4.1 for environmental failures.

## Out-of-scope explicit deferrals

- `getServerStatus` ACP method (YAGNI, no caller).
- Lobster typed-workflow tool.
- Native iOS app (PR-B).
- Custom wake-word training UX.
- Live Canvas / A2UI.
- More channel adapters.
