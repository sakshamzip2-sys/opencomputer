# Reasoning Dropdown v2 — Retroactive Expand + Tree View

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/reasoning show` actually expand the most recent (or any past) thinking block, rendered as a structured tree that includes thinking text + tool actions taken — like Claude.ai's web-UI thought-process dropdown.

**Architecture:** Introduce a session-scoped `ReasoningStore` that captures one `ReasoningTurn` per chat turn (turn id, thinking text, ordered tool actions, durations). The `StreamingRenderer` pushes its captured state into the store on `finalize()`. The `/reasoning show [N|all|last]` slash command queries the store and prints a Rich `Tree` to the console. Updated collapsed-line format includes the turn id so users can refer to it explicitly. A new `Ctrl+R` key binding in `input_loop.py` triggers `/reasoning show` for the last turn without typing.

**Tech Stack:** Python 3.12+, Rich (Tree, Group, Panel, Text), prompt_toolkit (key bindings), pytest.

**Out of scope (explicit):**
- Textual migration / true interactive collapsible widgets — deferred per the v1 spec.
- Streaming-time tree updates — tree view is for finalized turns only.
- Persisting reasoning across CLI restarts — in-memory only; resume comes later.
- Capturing tool *outputs* in the tree — only tool *names + args preview + ok status* are stored.
- ANSI color in `/reasoning show` output — slash output flows through `result.output` (a plain string) which becomes a chat message; emitting ANSI codes there renders as garbage in some pathways. Use plain-text Unicode tree connectors instead (still very readable: `├──`, `└──`).

**Audit-confirmed assumptions (verified before this plan was finalized):**
- `RuntimeContext` is `frozen=True, slots=True` BUT `custom: dict[str, Any]` is mutable — assigning `runtime.custom["_reasoning_store"] = store` works because we mutate the dict, not the field.
- `cli.py:939` constructs ONE `RuntimeContext` per chat session (verified via grep). It is reused across turns at `cli.py:1116`. So `runtime.custom` IS shared across turns and is the right home for the store.
- `SlashCommandResult.output` is plain text (line 172 of `opencomputer/agent/slash_commands.py` returns it as-is; loop.py wraps it as message content). Pre-rendering Rich Tree to a plain-text buffer is the correct approach.
- Input loop uses `prompt_toolkit` with `Keys.ControlX, Keys.ControlE` chord style (e.g. `input_loop.py:209`). The keybinding in Task 8 uses the same chord pattern (`Keys.ControlX, Keys.ControlR`) to avoid stomping `Ctrl+R` (prompt_toolkit's emacs reverse-search default).
- `tests/test_reasoning_persistence.py` and `tests/test_reasoning_replay_blocks.py` exist but contain ZERO references to `show/hide/status` (verified via grep). They cover SessionDB-side reasoning persistence — orthogonal to this plan. Run them in Task 6 step 5 as a regression smoke.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `opencomputer/cli_ui/reasoning_store.py` | **Create** | `ReasoningTurn` dataclass + `ReasoningStore` class (in-memory, capped to last N turns) |
| `opencomputer/cli_ui/streaming.py` | Modify | Add `_tool_history` parallel unbounded list; accept `reasoning_store` via `__init__`; push on `finalize`; update collapsed-line format |
| `opencomputer/cli_ui/__init__.py` | Modify | Export `ReasoningStore`, `ReasoningTurn` |
| `opencomputer/agent/slash_commands_impl/reasoning_cmd.py` | Modify | Implement `show [N\|all\|last]` retrieval + Rich Tree rendering using the captured store |
| `opencomputer/cli.py` | Modify | Instantiate `ReasoningStore` once per session; stash on `runtime.custom["_reasoning_store"]`; pass to renderer per turn |
| `opencomputer/cli_ui/input_loop.py` | Modify | Add `Ctrl+R` key binding → enqueues `/reasoning show last` |
| `tests/test_reasoning_store.py` | **Create** | Unit tests for store: append, get_by_id, get_latest, get_all, capping |
| `tests/test_reasoning_show_retroactive.py` | **Create** | Slash-command behavior: show last / show N / show all / unknown N / store-empty |
| `tests/test_streaming_thinking.py` | Modify | Cover new `_tool_history` + push-to-store on finalize |
| `tests/test_thinking_dropdown_e2e.py` | Modify | Update collapsed-line assertions to include turn id |

---

## Task 0: Worktree + branch setup

**Per user's hard rule (memory: feedback_worktrees_for_parallel_sessions):** never share a working tree on a branch with another Claude session. Set up an isolated worktree before any code changes.

- [ ] **Step 1: Create worktree on a fresh branch**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
# The parent git repo is at /Users/saksham/Vscode/claude/. Worktree below it.
git -C /Users/saksham/Vscode/claude worktree add \
    /Users/saksham/.config/superpowers/worktrees/claude/reasoning-dropdown-v2 \
    -b feat/reasoning-dropdown-v2 main
```

- [ ] **Step 2: cd into the worktree for the rest of the plan**

```bash
cd /Users/saksham/.config/superpowers/worktrees/claude/reasoning-dropdown-v2/OpenComputer
ls -la opencomputer/cli_ui/streaming.py  # sanity check the worktree mirrors main
```

- [ ] **Step 3: Activate the same venv (lives in main checkout, shared)**

```bash
source /Users/saksham/Vscode/claude/OpenComputer/.venv/bin/activate
python -c "import opencomputer; print(opencomputer.__version__)"
```

- [ ] **Step 4: Smoke-run the existing test suite from the worktree**

```bash
pytest tests/ -q 2>&1 | tail -5
```

Expected: green (matches the 885-test baseline noted in CLAUDE.md). Establishes a clean starting point.

> All subsequent tasks operate **inside this worktree**. The final push (Task 11 step 3) pushes the `feat/reasoning-dropdown-v2` branch.

---

## Task 1: ReasoningStore + ReasoningTurn data structures

**Files:**
- Create: `opencomputer/cli_ui/reasoning_store.py`
- Test: `tests/test_reasoning_store.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reasoning_store.py
"""Unit tests for ReasoningStore (per-session in-memory store)."""
from __future__ import annotations

import pytest

from opencomputer.cli_ui.reasoning_store import (
    ReasoningStore,
    ReasoningTurn,
    ToolAction,
)


def test_store_starts_empty():
    store = ReasoningStore()
    assert store.get_all() == []
    assert store.get_latest() is None


def test_append_assigns_monotonic_turn_ids():
    store = ReasoningStore()
    t1 = store.append(thinking="first", duration_s=0.5, tool_actions=[])
    t2 = store.append(thinking="second", duration_s=1.2, tool_actions=[])
    assert t1.turn_id == 1
    assert t2.turn_id == 2
    assert store.get_latest() is t2


def test_get_by_id_returns_match_or_none():
    store = ReasoningStore()
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    store.append(thinking="y", duration_s=0.2, tool_actions=[])
    assert store.get_by_id(1).thinking == "x"
    assert store.get_by_id(2).thinking == "y"
    assert store.get_by_id(99) is None


def test_store_caps_to_max_turns():
    store = ReasoningStore(max_turns=3)
    for i in range(5):
        store.append(thinking=f"t{i}", duration_s=0.1, tool_actions=[])
    all_turns = store.get_all()
    assert len(all_turns) == 3
    # Oldest two evicted; turn_ids 3, 4, 5 remain.
    assert [t.turn_id for t in all_turns] == [3, 4, 5]
    # get_by_id for an evicted turn returns None.
    assert store.get_by_id(1) is None


def test_tool_action_is_immutable_record():
    a = ToolAction(name="Read", args_preview="foo.py", ok=True, duration_s=0.05)
    with pytest.raises(Exception):
        a.name = "Edit"  # type: ignore[misc]


def test_reasoning_turn_records_action_count():
    actions = [
        ToolAction(name="Read", args_preview="a.py", ok=True, duration_s=0.1),
        ToolAction(name="Edit", args_preview="b.py", ok=True, duration_s=0.2),
    ]
    store = ReasoningStore()
    turn = store.append(thinking="reasoning", duration_s=1.0, tool_actions=actions)
    assert turn.action_count == 2
    assert turn.tool_actions[0].name == "Read"


def test_empty_thinking_still_records_turn():
    """Tool-only turns (no extended-thinking) must still be queryable."""
    store = ReasoningStore()
    turn = store.append(thinking="", duration_s=0.5, tool_actions=[
        ToolAction(name="Bash", args_preview="ls", ok=True, duration_s=0.05),
    ])
    assert turn.turn_id == 1
    assert turn.thinking == ""
    assert turn.action_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
pytest tests/test_reasoning_store.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'opencomputer.cli_ui.reasoning_store'`

- [ ] **Step 3: Implement the store**

```python
# opencomputer/cli_ui/reasoning_store.py
"""Per-session in-memory store of finalized reasoning turns.

Captured at :meth:`StreamingRenderer.finalize`, queried by the
``/reasoning show`` slash command. Survives across chat turns within a
single CLI session; not persisted to disk.

Capped to the last ``max_turns`` to keep memory bounded for long
sessions. Eviction is FIFO (oldest first); evicted turns return ``None``
from :meth:`get_by_id`.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


_DEFAULT_MAX_TURNS = 50


@dataclass(frozen=True)
class ToolAction:
    """One tool dispatch within a turn. Immutable record."""

    name: str
    args_preview: str
    ok: bool
    duration_s: float


@dataclass(frozen=True)
class ReasoningTurn:
    """A finalized chat turn's reasoning + tool-action trail."""

    turn_id: int
    thinking: str
    duration_s: float
    tool_actions: tuple[ToolAction, ...] = field(default_factory=tuple)

    @property
    def action_count(self) -> int:
        return len(self.tool_actions)


class ReasoningStore:
    """Append-only ring buffer of :class:`ReasoningTurn`.

    Thread-safety: NOT thread-safe. The CLI chat loop is single-threaded
    so this is fine; if a future caller needs concurrent appends, wrap
    accesses with a lock.
    """

    def __init__(self, max_turns: int = _DEFAULT_MAX_TURNS) -> None:
        self._turns: deque[ReasoningTurn] = deque(maxlen=max_turns)
        self._next_id = 1

    def append(
        self,
        *,
        thinking: str,
        duration_s: float,
        tool_actions: list[ToolAction] | tuple[ToolAction, ...],
    ) -> ReasoningTurn:
        turn = ReasoningTurn(
            turn_id=self._next_id,
            thinking=thinking,
            duration_s=duration_s,
            tool_actions=tuple(tool_actions),
        )
        self._next_id += 1
        self._turns.append(turn)
        return turn

    def get_latest(self) -> ReasoningTurn | None:
        return self._turns[-1] if self._turns else None

    def get_by_id(self, turn_id: int) -> ReasoningTurn | None:
        for t in self._turns:
            if t.turn_id == turn_id:
                return t
        return None

    def get_all(self) -> list[ReasoningTurn]:
        return list(self._turns)


__all__ = ["ReasoningStore", "ReasoningTurn", "ToolAction"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_reasoning_store.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_ui/reasoning_store.py tests/test_reasoning_store.py
git commit -m "feat(cli_ui): add ReasoningStore for per-session reasoning capture"
```

---

## Task 2: Renderer captures tool history (parallel unbounded list)

**Files:**
- Modify: `opencomputer/cli_ui/streaming.py` (`__init__`, `on_tool_start`, `on_tool_end`)
- Test: `tests/test_streaming_thinking.py`

The current `_tool_calls` OrderedDict caps visible rows at 3 (eviction at lines 189-190). For the tree view, we need every tool call from the turn — add a parallel unbounded `_tool_history` list that records each call as it completes.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_streaming_thinking.py`:

```python
def test_renderer_records_unbounded_tool_history():
    """Tool-call panel evicts after 3 visible rows; the parallel
    history must keep ALL of them so the reasoning tree can render
    the full action sequence."""
    from rich.console import Console

    from opencomputer.cli_ui.streaming import StreamingRenderer

    renderer = StreamingRenderer(Console(file=__import__("io").StringIO()))
    with renderer:
        for i in range(5):
            idx = renderer.on_tool_start(f"Tool{i}", f"arg{i}")
            renderer.on_tool_end(f"Tool{i}", idx, ok=(i % 2 == 0))

    history = renderer.tool_history()
    assert [a.name for a in history] == [f"Tool{i}" for i in range(5)]
    assert [a.ok for a in history] == [True, False, True, False, True]
    # Visible panel still capped at 3.
    assert len(renderer._tool_calls) == 3
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_streaming_thinking.py::test_renderer_records_unbounded_tool_history -v
```

Expected: FAIL with `AttributeError: 'StreamingRenderer' object has no attribute 'tool_history'`

- [ ] **Step 3: Add `_tool_history` + `tool_history()` to StreamingRenderer**

In `opencomputer/cli_ui/streaming.py`:

a) Add import at top alongside existing dataclass import:

```python
from opencomputer.cli_ui.reasoning_store import ToolAction
```

b) In `__init__` (around line 107, after `self._tool_call_seq = 0`), add:

```python
        # Unbounded parallel history of completed tool calls. Used by
        # the reasoning tree (which needs the full sequence, not the
        # last-3 visible window).
        self._tool_history: list[ToolAction] = []
```

c) In `on_tool_end` (around line 194-202), append to history before returning. Replace the method body:

```python
    def on_tool_end(self, name: str, idx: int, ok: bool) -> None:
        """Mark a tool call as completed. Idempotent — late callbacks
        for evicted rows are silently dropped from the visible panel
        but ALWAYS captured in :attr:`_tool_history` for the reasoning
        tree.
        """
        row = self._tool_calls.get((name, idx))
        if row is not None:
            row.ended_at = time.monotonic()
            row.ok = ok
            duration = row.ended_at - row.started_at
            args_preview = row.args_preview
        else:
            # Row was evicted from the visible panel before the end
            # callback arrived. Synthesize a minimal record for the
            # history so the tree still shows it.
            duration = 0.0
            args_preview = ""
        self._tool_history.append(
            ToolAction(
                name=name,
                args_preview=args_preview,
                ok=ok,
                duration_s=duration,
            )
        )
        self._refresh()
```

d) Add accessor below `on_tool_end`:

```python
    def tool_history(self) -> list[ToolAction]:
        """Return the full ordered list of completed tool calls this
        turn. Includes calls evicted from the visible panel.
        """
        return list(self._tool_history)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_streaming_thinking.py::test_renderer_records_unbounded_tool_history -v
```

Expected: PASS.

- [ ] **Step 5: Run the full streaming test file to confirm no regressions**

```bash
pytest tests/test_streaming_thinking.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/cli_ui/streaming.py tests/test_streaming_thinking.py
git commit -m "feat(cli_ui): record unbounded tool history alongside the 3-row visible panel"
```

---

## Task 3: Renderer pushes captured state into ReasoningStore on finalize

**Files:**
- Modify: `opencomputer/cli_ui/streaming.py` (`__init__` accepts store; `finalize` pushes)
- Test: `tests/test_streaming_thinking.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_streaming_thinking.py`:

```python
def test_finalize_pushes_turn_into_reasoning_store():
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import ReasoningStore
    from opencomputer.cli_ui.streaming import StreamingRenderer

    store = ReasoningStore()
    renderer = StreamingRenderer(
        Console(file=io.StringIO()), reasoning_store=store
    )
    with renderer:
        renderer.on_thinking_chunk("Let me ")
        renderer.on_thinking_chunk("think...")
        idx = renderer.on_tool_start("Read", "foo.py")
        renderer.on_tool_end("Read", idx, ok=True)
        renderer.finalize(
            reasoning="Let me think...",
            iterations=1,
            in_tok=10,
            out_tok=20,
            elapsed_s=1.5,
            show_reasoning=False,
        )

    turn = store.get_latest()
    assert turn is not None
    assert turn.turn_id == 1
    assert turn.thinking == "Let me think..."
    assert turn.action_count == 1
    assert turn.tool_actions[0].name == "Read"


def test_finalize_skips_store_push_when_no_store_attached():
    """Backwards compat: existing callers that don't pass a store must
    keep working without crashing."""
    import io

    from rich.console import Console

    from opencomputer.cli_ui.streaming import StreamingRenderer

    renderer = StreamingRenderer(Console(file=io.StringIO()))  # no store
    with renderer:
        renderer.finalize(
            reasoning="x",
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.1,
            show_reasoning=False,
        )
    # No exception; nothing else to assert.


def test_finalize_records_turn_even_without_thinking():
    """Tool-only turns (no extended-thinking) must still be recorded
    so /reasoning show all shows them."""
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import ReasoningStore
    from opencomputer.cli_ui.streaming import StreamingRenderer

    store = ReasoningStore()
    renderer = StreamingRenderer(Console(file=io.StringIO()), reasoning_store=store)
    with renderer:
        idx = renderer.on_tool_start("Bash", "ls")
        renderer.on_tool_end("Bash", idx, ok=True)
        renderer.finalize(
            reasoning=None,
            iterations=1,
            in_tok=5,
            out_tok=5,
            elapsed_s=0.5,
            show_reasoning=False,
        )
    turn = store.get_latest()
    assert turn is not None
    assert turn.thinking == ""
    assert turn.action_count == 1


def test_finalize_skips_empty_no_op_turn():
    """A turn with neither thinking nor tool calls is a no-op and
    should NOT pollute /reasoning show all with empty entries."""
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import ReasoningStore
    from opencomputer.cli_ui.streaming import StreamingRenderer

    store = ReasoningStore()
    renderer = StreamingRenderer(Console(file=io.StringIO()), reasoning_store=store)
    with renderer:
        renderer.finalize(
            reasoning=None,
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.1,
            show_reasoning=False,
        )
    assert store.get_all() == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_streaming_thinking.py::test_finalize_pushes_turn_into_reasoning_store tests/test_streaming_thinking.py::test_finalize_skips_store_push_when_no_store_attached tests/test_streaming_thinking.py::test_finalize_records_turn_even_without_thinking -v
```

Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'reasoning_store'`

- [ ] **Step 3: Wire the store into the renderer**

In `opencomputer/cli_ui/streaming.py`:

a) Update `__init__` signature (line 93):

```python
    def __init__(
        self,
        console: Console,
        *,
        reasoning_store: "ReasoningStore | None" = None,
    ) -> None:
        self.console = console
        self._reasoning_store = reasoning_store
        # ... rest unchanged
```

Add the forward-ref import in the TYPE_CHECKING block (line 41):

```python
if TYPE_CHECKING:
    from opencomputer.cli_ui.reasoning_store import ReasoningStore
```

b) At the end of `finalize()` (after the token-rate footer print, line 286), append:

```python
        # Push captured state into the per-session store so the
        # /reasoning show command can re-render this turn later.
        # Skip turns that are pure no-ops (no thinking, no tools) — they
        # add noise to /reasoning show all without information value.
        if self._reasoning_store is not None:
            thinking_str = (reasoning or "").strip()
            if thinking_str or self._tool_history:
                thinking_elapsed_for_store = (
                    (time.monotonic() - self._thinking_started_at)
                    if self._thinking_started_at > 0.0
                    else elapsed_s
                )
                self._reasoning_store.append(
                    thinking=thinking_str,
                    duration_s=thinking_elapsed_for_store,
                    tool_actions=self._tool_history,
                )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_streaming_thinking.py -v
```

Expected: all pass (including the three new tests).

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_ui/streaming.py tests/test_streaming_thinking.py
git commit -m "feat(cli_ui): push finalized turn into ReasoningStore for later replay"
```

---

## Task 4: Updated collapsed-line format includes turn id + action count

**Files:**
- Modify: `opencomputer/cli_ui/streaming.py` (collapsed-line branch in `finalize`)
- Test: `tests/test_streaming_thinking.py`

Old format: `💭 Thought for 0.8s — /reasoning show to expand`
New format: `💭 Thought for 0.8s · turn #5 · 3 actions — /reasoning show to expand`

When the renderer has no store attached, omit the turn id (no way to know it).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_streaming_thinking.py`:

```python
def test_collapsed_line_includes_turn_id_and_action_count():
    import io
    import re

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import ReasoningStore
    from opencomputer.cli_ui.streaming import StreamingRenderer

    out = io.StringIO()
    store = ReasoningStore()
    renderer = StreamingRenderer(Console(file=out, force_terminal=False), reasoning_store=store)
    with renderer:
        renderer.on_thinking_chunk("hmm")
        idx1 = renderer.on_tool_start("Read", "a")
        renderer.on_tool_end("Read", idx1, ok=True)
        idx2 = renderer.on_tool_start("Edit", "b")
        renderer.on_tool_end("Edit", idx2, ok=True)
        renderer.finalize(
            reasoning="hmm",
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.1,
            show_reasoning=False,
        )
    text = out.getvalue()
    # Match: "Thought for <duration> · turn #1 · 2 actions — /reasoning show to expand"
    assert re.search(r"turn #1", text), text
    assert re.search(r"2 actions", text), text
    assert "/reasoning show to expand" in text


def test_collapsed_line_omits_turn_id_when_store_missing():
    """Backwards compat: legacy callers without a store keep the old format."""
    import io

    from rich.console import Console

    from opencomputer.cli_ui.streaming import StreamingRenderer

    out = io.StringIO()
    renderer = StreamingRenderer(Console(file=out, force_terminal=False))
    with renderer:
        renderer.on_thinking_chunk("hmm")
        renderer.finalize(
            reasoning="hmm",
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.1,
            show_reasoning=False,
        )
    text = out.getvalue()
    assert "turn #" not in text
    assert "/reasoning show to expand" in text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_streaming_thinking.py::test_collapsed_line_includes_turn_id_and_action_count tests/test_streaming_thinking.py::test_collapsed_line_omits_turn_id_when_store_missing -v
```

Expected: FAIL — first test fails because format doesn't include "turn #1"; second test passes already.

- [ ] **Step 3: Update the collapsed-line format**

In `opencomputer/cli_ui/streaming.py`, replace the `else:` branch in `finalize` (lines 261-266) with:

```python
            else:
                # Collapsed format. When a store is attached, prefix the
                # turn id + action count so users can refer to it
                # explicitly: "/reasoning show 5".
                next_turn_id = (
                    self._reasoning_store._next_id  # type: ignore[union-attr]
                    if self._reasoning_store is not None
                    else None
                )
                action_count = len(self._tool_history)
                meta_parts: list[str] = [
                    f"💭 Thought for {_fmt_duration(thinking_elapsed)}"
                ]
                if next_turn_id is not None:
                    meta_parts.append(f"turn #{next_turn_id}")
                if action_count > 0:
                    s = "" if action_count == 1 else "s"
                    meta_parts.append(f"{action_count} action{s}")
                meta = " · ".join(meta_parts)
                self.console.print(
                    f"[dim cyan]{meta} — /reasoning show to expand[/dim cyan]"
                )
```

NOTE on `_next_id` access: the displayed id is what the about-to-be-pushed turn will get. To avoid reaching into `_next_id` (private), expose a `peek_next_id()` method on the store first.

In `reasoning_store.py`, add:

```python
    def peek_next_id(self) -> int:
        """Return the id the next :meth:`append` will assign.

        Lets the renderer print the turn id in the collapsed line BEFORE
        the push happens (the push is the last step of finalize).
        """
        return self._next_id
```

And use it in streaming.py instead of `_next_id`:

```python
                next_turn_id = (
                    self._reasoning_store.peek_next_id()
                    if self._reasoning_store is not None
                    else None
                )
```

- [ ] **Step 4: Add a unit test for `peek_next_id`**

Append to `tests/test_reasoning_store.py`:

```python
def test_peek_next_id_returns_id_of_next_append():
    store = ReasoningStore()
    assert store.peek_next_id() == 1
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    assert store.peek_next_id() == 2
```

- [ ] **Step 5: Run all changed tests**

```bash
pytest tests/test_reasoning_store.py tests/test_streaming_thinking.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/cli_ui/reasoning_store.py opencomputer/cli_ui/streaming.py tests/test_reasoning_store.py tests/test_streaming_thinking.py
git commit -m "feat(cli_ui): collapsed reasoning line shows turn id + action count"
```

---

## Task 5: Tree-style render helper

**Files:**
- Modify: `opencomputer/cli_ui/reasoning_store.py` (add `render_turn_tree`)
- Test: `tests/test_reasoning_store.py`

Render one `ReasoningTurn` as a Rich `Tree`:

```
💭 Turn #1 · Thought for 0.8s · 3 actions
├── 🧠 Reasoning: Let me think about how to do this...
├── 🔧 Read(foo.py) ✓ 0.05s
├── 🔧 Edit(bar.py) ✓ 0.12s
└── 🔧 Bash(ls) ✗ 0.03s
```

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reasoning_store.py`:

```python
def test_render_turn_tree_returns_rich_tree_with_expected_nodes():
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import (
        ReasoningStore,
        ToolAction,
        render_turn_tree,
    )

    store = ReasoningStore()
    turn = store.append(
        thinking="Let me think about how to do this carefully.",
        duration_s=0.8,
        tool_actions=[
            ToolAction(name="Read", args_preview="foo.py", ok=True, duration_s=0.05),
            ToolAction(name="Edit", args_preview="bar.py", ok=True, duration_s=0.12),
            ToolAction(name="Bash", args_preview="ls", ok=False, duration_s=0.03),
        ],
    )

    tree = render_turn_tree(turn)
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(tree)
    text = out.getvalue()

    assert "Turn #1" in text
    assert "Thought for" in text
    assert "3 actions" in text
    assert "Let me think about" in text
    assert "Read" in text and "foo.py" in text
    assert "Edit" in text and "bar.py" in text
    assert "Bash" in text and "ls" in text
    # Failed call indicator.
    assert "✗" in text or "FAIL" in text


def test_render_turn_tree_handles_no_thinking():
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import (
        ReasoningStore,
        ToolAction,
        render_turn_tree,
    )

    store = ReasoningStore()
    turn = store.append(
        thinking="",
        duration_s=0.2,
        tool_actions=[
            ToolAction(name="Bash", args_preview="ls", ok=True, duration_s=0.05),
        ],
    )
    tree = render_turn_tree(turn)
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(tree)
    text = out.getvalue()
    assert "Turn #1" in text
    assert "Bash" in text
    # No reasoning child node when thinking is empty.
    assert "Reasoning:" not in text
    assert "(no extended thinking)" in text


def test_render_turn_tree_handles_no_actions():
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import (
        ReasoningStore,
        render_turn_tree,
    )

    store = ReasoningStore()
    turn = store.append(thinking="just thinking", duration_s=0.5, tool_actions=[])
    tree = render_turn_tree(turn)
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(tree)
    text = out.getvalue()
    assert "Turn #1" in text
    assert "just thinking" in text
    assert "(no tool actions)" in text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_reasoning_store.py -v
```

Expected: FAIL with `ImportError: cannot import name 'render_turn_tree'`

- [ ] **Step 3: Implement `render_turn_tree`**

Append to `opencomputer/cli_ui/reasoning_store.py`:

```python
from rich.text import Text
from rich.tree import Tree


def _fmt_duration(seconds: float) -> str:
    """Match streaming.py's duration formatter."""
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def render_turn_tree(turn: ReasoningTurn) -> Tree:
    """Render one ReasoningTurn as a Rich Tree for the console.

    Layout::

        💭 Turn #N · Thought for X.Xs · K actions
        ├── 🧠 Reasoning: <thinking text>
        ├── 🔧 Tool(args) ✓ 0.05s
        └── 🔧 Tool(args) ✗ 0.12s

    Empty thinking and empty tool-action lists each get a single
    placeholder child so users see the structure, not just a header.
    """
    s = "" if turn.action_count == 1 else "s"
    header = Text.assemble(
        ("💭 ", "dim cyan"),
        (f"Turn #{turn.turn_id}", "bold cyan"),
        ("  ·  ", "dim"),
        (f"Thought for {_fmt_duration(turn.duration_s)}", "dim cyan"),
        ("  ·  ", "dim"),
        (f"{turn.action_count} action{s}", "dim cyan"),
    )
    tree = Tree(header, guide_style="grey50")

    if turn.thinking:
        # Indent multi-line thinking under a single "Reasoning:" node so
        # the tree connectors stay clean.
        thinking_node = tree.add(Text.assemble(("🧠 Reasoning: ", "dim cyan")))
        for line in turn.thinking.splitlines() or [turn.thinking]:
            thinking_node.add(Text(line, style="dim"))
    else:
        tree.add(Text("(no extended thinking)", style="italic dim"))

    if turn.tool_actions:
        for action in turn.tool_actions:
            mark = "✓" if action.ok else "✗"
            mark_style = "green" if action.ok else "red"
            tree.add(
                Text.assemble(
                    ("🔧 ", "dim"),
                    (action.name, "bold"),
                    ("(", "dim"),
                    (action.args_preview, "dim"),
                    (") ", "dim"),
                    (mark, mark_style),
                    (f"  {_fmt_duration(action.duration_s)}", "dim"),
                )
            )
    else:
        tree.add(Text("(no tool actions)", style="italic dim"))

    return tree


__all__ = [
    "ReasoningStore",
    "ReasoningTurn",
    "ToolAction",
    "render_turn_tree",
]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_reasoning_store.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_ui/reasoning_store.py tests/test_reasoning_store.py
git commit -m "feat(cli_ui): render_turn_tree — Rich Tree view of one ReasoningTurn"
```

---

## Task 6: `/reasoning show [N|all|last]` retroactive command

**Files:**
- Modify: `opencomputer/agent/slash_commands_impl/reasoning_cmd.py`
- Test: `tests/test_reasoning_show_retroactive.py` (create)

The slash command currently sets `runtime.custom["show_reasoning"] = True` for the next turn. Extend it: when no second argument or `last`, render the latest turn from the store as a tree. With `<N>`, render that turn id. With `all`, render every turn.

The `SlashCommandResult.output` is plain text printed by the dispatcher. To embed Rich-formatted output, render the tree to a string via `Console.capture()`.

The store is found via `runtime.custom["_reasoning_store"]` (wired in Task 8).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reasoning_show_retroactive.py`:

```python
"""Behavioral tests for /reasoning show — retroactive expand."""
from __future__ import annotations

import asyncio

import pytest

from opencomputer.agent.slash_commands_impl.reasoning_cmd import ReasoningCommand
from opencomputer.cli_ui.reasoning_store import ReasoningStore, ToolAction
from plugin_sdk.runtime_context import RuntimeContext


def _runtime_with_store(store: ReasoningStore | None) -> RuntimeContext:
    rt = RuntimeContext()
    if store is not None:
        rt.custom["_reasoning_store"] = store
    return rt


def _run(cmd: ReasoningCommand, args: str, runtime: RuntimeContext):
    return asyncio.run(cmd.execute(args, runtime))


def test_show_with_no_store_returns_helpful_message():
    cmd = ReasoningCommand()
    rt = _runtime_with_store(None)
    res = _run(cmd, "show", rt)
    assert res.handled
    assert "no reasoning history" in res.output.lower() or "not available" in res.output.lower()


def test_show_when_store_empty_returns_helpful_message():
    cmd = ReasoningCommand()
    rt = _runtime_with_store(ReasoningStore())
    res = _run(cmd, "show", rt)
    assert res.handled
    assert "no" in res.output.lower()  # "no turns yet" or similar


def test_show_renders_latest_turn_as_tree():
    cmd = ReasoningCommand()
    store = ReasoningStore()
    store.append(
        thinking="first turn thinking",
        duration_s=0.5,
        tool_actions=[ToolAction(name="Read", args_preview="a.py", ok=True, duration_s=0.1)],
    )
    store.append(
        thinking="second turn thinking",
        duration_s=0.7,
        tool_actions=[ToolAction(name="Edit", args_preview="b.py", ok=True, duration_s=0.2)],
    )
    rt = _runtime_with_store(store)
    res = _run(cmd, "show", rt)
    assert res.handled
    out = res.output
    assert "Turn #2" in out
    assert "second turn thinking" in out
    assert "Edit" in out
    # Latest only — first turn must not appear.
    assert "Turn #1" not in out


def test_show_last_is_alias_for_show():
    cmd = ReasoningCommand()
    store = ReasoningStore()
    store.append(thinking="t", duration_s=0.1, tool_actions=[])
    rt = _runtime_with_store(store)
    res = _run(cmd, "show last", rt)
    assert res.handled
    assert "Turn #1" in res.output


def test_show_specific_turn_by_id():
    cmd = ReasoningCommand()
    store = ReasoningStore()
    store.append(thinking="alpha", duration_s=0.1, tool_actions=[])
    store.append(thinking="beta", duration_s=0.1, tool_actions=[])
    store.append(thinking="gamma", duration_s=0.1, tool_actions=[])
    rt = _runtime_with_store(store)
    res = _run(cmd, "show 2", rt)
    assert res.handled
    assert "Turn #2" in res.output
    assert "beta" in res.output
    assert "alpha" not in res.output
    assert "gamma" not in res.output


def test_show_unknown_turn_id_returns_error():
    cmd = ReasoningCommand()
    store = ReasoningStore()
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    rt = _runtime_with_store(store)
    res = _run(cmd, "show 99", rt)
    assert res.handled
    assert "no turn" in res.output.lower() or "unknown" in res.output.lower()


def test_show_all_renders_every_turn():
    cmd = ReasoningCommand()
    store = ReasoningStore()
    store.append(thinking="alpha", duration_s=0.1, tool_actions=[])
    store.append(thinking="beta", duration_s=0.1, tool_actions=[])
    rt = _runtime_with_store(store)
    res = _run(cmd, "show all", rt)
    assert res.handled
    assert "Turn #1" in res.output
    assert "Turn #2" in res.output
    assert "alpha" in res.output
    assert "beta" in res.output


def test_legacy_show_still_sets_flag_for_next_turn():
    """Backwards compat: existing test_reasoning_persistence.py expects
    show to also flip the flag so streaming providers expose the
    raw <think> text on the NEXT turn. Both behaviors must coexist.
    """
    cmd = ReasoningCommand()
    rt = _runtime_with_store(ReasoningStore())
    _ = _run(cmd, "show", rt)
    assert rt.custom.get("show_reasoning") is True


def test_hide_still_clears_flag():
    cmd = ReasoningCommand()
    rt = _runtime_with_store(None)
    rt.custom["show_reasoning"] = True
    res = _run(cmd, "hide", rt)
    assert res.handled
    assert rt.custom.get("show_reasoning") is False


def test_status_unchanged():
    cmd = ReasoningCommand()
    rt = _runtime_with_store(None)
    res = _run(cmd, "status", rt)
    assert res.handled
    assert "effort=" in res.output


def test_level_setting_unchanged():
    cmd = ReasoningCommand()
    rt = _runtime_with_store(None)
    res = _run(cmd, "high", rt)
    assert res.handled
    assert "high" in res.output
    assert rt.custom.get("reasoning_effort") == "high"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_reasoning_show_retroactive.py -v
```

Expected: many fail (current command doesn't render trees from store).

- [ ] **Step 3: Implement the new behavior**

Replace `opencomputer/agent/slash_commands_impl/reasoning_cmd.py`:

```python
"""``/reasoning [level|show [N|all|last]|hide|status]`` — control thinking display + effort.

Three orthogonal knobs:

1. **Effort level** (sets ``runtime.custom["reasoning_effort"]``):
   ``none``, ``minimal``, ``low``, ``medium``, ``high``, ``xhigh``.

2. **Display toggle for FUTURE turns** (sets ``runtime.custom["show_reasoning"]``):
   ``show`` reveals streamed ``<think>`` blocks; ``hide`` strips them.
   Default: hidden.

3. **Retroactive expand of PAST turns** (reads ``runtime.custom["_reasoning_store"]``):
   ``show`` (or ``show last``) prints the most recent turn as a tree.
   ``show <N>`` prints turn N. ``show all`` prints every turn in the store.

Examples::

    /reasoning              → status
    /reasoning high         → set effort to high
    /reasoning show         → expand the LAST turn AND show <think> on next turns
    /reasoning show 5       → expand turn #5 only
    /reasoning show all     → expand every turn in the store
    /reasoning hide         → hide <think> on future turns
    /reasoning none         → disable reasoning entirely
"""

from __future__ import annotations

import io
import re

from rich.console import Console

from opencomputer.cli_ui.reasoning_store import ReasoningStore, render_turn_tree
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_VALID_LEVELS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)
_DEFAULT_LEVEL = "medium"

_USAGE = (
    "Usage: /reasoning [level|show [N|all|last]|hide|status]\n"
    "  Levels: none, minimal, low, medium, high, xhigh\n"
    "  show          → expand the last turn (and show <think> next turns)\n"
    "  show <N>      → expand turn #N\n"
    "  show all      → expand every turn in the store\n"
    "  hide          → hide <think> on future turns\n"
    "  status        → show current settings"
)


def _current_level(runtime: RuntimeContext) -> str:
    return str(runtime.custom.get("reasoning_effort", _DEFAULT_LEVEL))


def _current_show(runtime: RuntimeContext) -> bool:
    return bool(runtime.custom.get("show_reasoning", False))


def _format_status(runtime: RuntimeContext) -> str:
    level = _current_level(runtime)
    show = "shown" if _current_show(runtime) else "hidden"
    return f"reasoning: effort={level}, display={show}"


def _get_store(runtime: RuntimeContext) -> ReasoningStore | None:
    candidate = runtime.custom.get("_reasoning_store")
    return candidate if isinstance(candidate, ReasoningStore) else None


def _render_to_string(*trees) -> str:
    """Render one or more Rich Trees to plain text suitable for
    ``SlashCommandResult.output``.

    The dispatcher routes that string into the chat as a message
    (see ``opencomputer/agent/loop.py:174``) — embedding ANSI escape
    sequences there causes garbled output along several render paths.
    So we render with ``color_system=None`` to suppress ANSI while
    keeping the Unicode tree connectors (``├──``, ``└──``) Rich draws
    by default. Width=120 is wide enough for typical terminals and
    avoids hard-wrapping deeply nested children.
    """
    buf = io.StringIO()
    console = Console(file=buf, color_system=None, width=120, no_color=True)
    for t in trees:
        console.print(t)
    return buf.getvalue().rstrip()


_SHOW_ID_PATTERN = re.compile(r"^show\s+(\d+)$")


class ReasoningCommand(SlashCommand):
    name = "reasoning"
    description = (
        "Show past reasoning + control reasoning effort + thinking-block display"
    )

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()

        if sub in ("", "status"):
            return SlashCommandResult(output=_format_status(runtime), handled=True)

        # --- show variants ---------------------------------------------------
        if sub == "show" or sub == "show last":
            runtime.custom["show_reasoning"] = True
            store = _get_store(runtime)
            if store is None:
                return SlashCommandResult(
                    output=(
                        "no reasoning history available "
                        "(store not attached to this session). "
                        f"{_format_status(runtime)}"
                    ),
                    handled=True,
                )
            turn = store.get_latest()
            if turn is None:
                return SlashCommandResult(
                    output="no reasoning turns recorded yet.",
                    handled=True,
                )
            return SlashCommandResult(
                output=_render_to_string(render_turn_tree(turn)), handled=True
            )

        if sub == "show all":
            store = _get_store(runtime)
            if store is None:
                return SlashCommandResult(
                    output="no reasoning history available (store not attached).",
                    handled=True,
                )
            turns = store.get_all()
            if not turns:
                return SlashCommandResult(
                    output="no reasoning turns recorded yet.",
                    handled=True,
                )
            return SlashCommandResult(
                output=_render_to_string(*[render_turn_tree(t) for t in turns]),
                handled=True,
            )

        m = _SHOW_ID_PATTERN.match(sub)
        if m:
            store = _get_store(runtime)
            if store is None:
                return SlashCommandResult(
                    output="no reasoning history available (store not attached).",
                    handled=True,
                )
            turn_id = int(m.group(1))
            turn = store.get_by_id(turn_id)
            if turn is None:
                return SlashCommandResult(
                    output=(
                        f"no turn #{turn_id} in store "
                        f"(known turns: {[t.turn_id for t in store.get_all()] or 'none'})."
                    ),
                    handled=True,
                )
            return SlashCommandResult(
                output=_render_to_string(render_turn_tree(turn)), handled=True
            )

        # --- legacy hide / level setters -------------------------------------
        if sub == "hide":
            runtime.custom["show_reasoning"] = False
            return SlashCommandResult(
                output=f"<think> blocks now HIDDEN. {_format_status(runtime)}",
                handled=True,
            )

        if sub in _VALID_LEVELS:
            runtime.custom["reasoning_effort"] = sub
            return SlashCommandResult(
                output=f"reasoning effort set to {sub}. {_format_status(runtime)}",
                handled=True,
            )

        return SlashCommandResult(output=_USAGE, handled=True)


__all__ = ["ReasoningCommand"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_reasoning_show_retroactive.py -v
```

Expected: all pass.

- [ ] **Step 5: Regression smoke for adjacent reasoning tests**

The existing `tests/test_reasoning_persistence.py` and `tests/test_reasoning_replay_blocks.py` (per CLAUDE.md/Explore agent) cover SessionDB-side reasoning persistence — orthogonal to this plan but they import from the same `runtime.custom["show_reasoning"]` flag. Confirm they still pass:

```bash
pytest tests/test_reasoning_persistence.py tests/test_reasoning_replay_blocks.py -v
```

Expected: all pass without modification.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/agent/slash_commands_impl/reasoning_cmd.py tests/test_reasoning_show_retroactive.py
git commit -m "feat(slash): /reasoning show [N|all|last] renders past turns as Rich Tree"
```

---

## Task 7: Wire ReasoningStore into the CLI session lifecycle

**Files:**
- Modify: `opencomputer/cli.py` (instantiate store at session start; pass to renderer; stash on runtime)
- Modify: `opencomputer/cli_ui/__init__.py` (re-export `ReasoningStore`)

- [ ] **Step 1: Re-export from cli_ui package**

In `opencomputer/cli_ui/__init__.py`, ensure these are exported:

```python
from opencomputer.cli_ui.reasoning_store import (
    ReasoningStore,
    ReasoningTurn,
    ToolAction,
    render_turn_tree,
)
from opencomputer.cli_ui.streaming import StreamingRenderer, current_renderer

__all__ = [
    "ReasoningStore",
    "ReasoningTurn",
    "ToolAction",
    "render_turn_tree",
    "StreamingRenderer",
    "current_renderer",
]
```

(If the file already has `__all__`, merge — don't replace existing entries.)

- [ ] **Step 2: Wire in cli.py**

Locate the chat loop in `opencomputer/cli.py`. Around line 1090-1110 the renderer is created per turn:

```python
from opencomputer.cli_ui import StreamingRenderer
with StreamingRenderer(console) as renderer:
    ...
    renderer.finalize(..., show_reasoning=runtime.custom.get("show_reasoning", False))
```

Two edits:

a) **Once per session** (find where `runtime` is constructed for the chat loop — search for `RuntimeContext(`). Add immediately after construction:

```python
        # One ReasoningStore per chat session — survives across turns,
        # accessed by /reasoning show and the renderer's finalize().
        from opencomputer.cli_ui import ReasoningStore
        if "_reasoning_store" not in runtime.custom:
            runtime.custom["_reasoning_store"] = ReasoningStore()
```

b) **Per turn**, pass the store to the renderer:

```python
        with StreamingRenderer(
            console,
            reasoning_store=runtime.custom.get("_reasoning_store"),
        ) as renderer:
```

- [ ] **Step 3: Add a CLI integration test**

Create `tests/test_reasoning_dropdown_integration.py`:

```python
"""End-to-end: a chat turn with thinking + tools makes /reasoning show work."""
from __future__ import annotations

import asyncio
import io

from rich.console import Console

from opencomputer.agent.slash_commands_impl.reasoning_cmd import ReasoningCommand
from opencomputer.cli_ui import ReasoningStore, StreamingRenderer
from plugin_sdk.runtime_context import RuntimeContext


def test_full_loop_session_store_renderer_then_show():
    """Simulates the cli.py wiring: one ReasoningStore for the whole
    session, renderer pushes per turn, /reasoning show retrieves."""
    runtime = RuntimeContext()
    runtime.custom["_reasoning_store"] = ReasoningStore()

    # Turn 1
    store = runtime.custom["_reasoning_store"]
    with StreamingRenderer(Console(file=io.StringIO()), reasoning_store=store) as r:
        r.on_thinking_chunk("turn one reasoning")
        idx = r.on_tool_start("Read", "a.py")
        r.on_tool_end("Read", idx, ok=True)
        r.finalize(
            reasoning="turn one reasoning",
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.3,
            show_reasoning=False,
        )

    # Turn 2 — fresh renderer, same store
    with StreamingRenderer(Console(file=io.StringIO()), reasoning_store=store) as r:
        r.on_thinking_chunk("turn two reasoning")
        r.finalize(
            reasoning="turn two reasoning",
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.4,
            show_reasoning=False,
        )

    # /reasoning show retrieves turn 2.
    cmd = ReasoningCommand()
    res = asyncio.run(cmd.execute("show", runtime))
    assert "Turn #2" in res.output
    assert "turn two reasoning" in res.output

    # /reasoning show 1 retrieves turn 1.
    res = asyncio.run(cmd.execute("show 1", runtime))
    assert "Turn #1" in res.output
    assert "turn one reasoning" in res.output
    assert "Read" in res.output

    # /reasoning show all retrieves both.
    res = asyncio.run(cmd.execute("show all", runtime))
    assert "Turn #1" in res.output
    assert "Turn #2" in res.output
```

- [ ] **Step 4: Run the integration test + full streaming/store/show suite**

```bash
pytest tests/test_reasoning_dropdown_integration.py tests/test_reasoning_store.py tests/test_streaming_thinking.py tests/test_reasoning_show_retroactive.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli.py opencomputer/cli_ui/__init__.py tests/test_reasoning_dropdown_integration.py
git commit -m "feat(cli): wire ReasoningStore into the chat session lifecycle"
```

---

## Task 8: `Ctrl+X Ctrl+R` chord triggers `/reasoning show`

**Files:**
- Modify: `opencomputer/cli_ui/input_loop.py`
- Test: `tests/test_input_loop_reasoning_keybinding.py` (create)

A keystroke for the most common case: "show me the last turn's reasoning, now". Avoids typing 16 characters.

**Why a chord, not a single key:** `Ctrl+R` alone is prompt_toolkit's emacs-mode reverse-search default — many users rely on it for command history. We use the chord pattern `Ctrl+X Ctrl+R`, mirroring the existing `Ctrl+X Ctrl+E` chord at `input_loop.py:209`. Chords don't conflict with single-key bindings and are conventionally used for "rare but useful" actions (vim/emacs both use this convention).

- [ ] **Step 1: Read the existing key bindings setup**

```bash
sed -n '160,260p' opencomputer/cli_ui/input_loop.py
```

Pin down the existing `KeyBindings` instance name (`kb` based on grep) and the `from prompt_toolkit.keys import Keys` import.

- [ ] **Step 2: Write the failing test**

Create `tests/test_input_loop_reasoning_keybinding.py`:

```python
"""The Ctrl+X Ctrl+R chord injects `/reasoning show` and submits."""
from __future__ import annotations

from opencomputer.cli_ui.input_loop import build_reasoning_show_handler


def test_handler_returns_slash_command_string():
    """The handler is the testable seam — when invoked, it returns the
    string the input_loop should inject as the user's next input."""
    out = build_reasoning_show_handler()()
    assert out == "/reasoning show"
```

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest tests/test_input_loop_reasoning_keybinding.py -v
```

Expected: FAIL — `build_reasoning_show_handler` doesn't exist.

- [ ] **Step 4: Add the handler factory + key binding**

In `opencomputer/cli_ui/input_loop.py`, add a top-level helper near the other module-level helpers:

```python
def build_reasoning_show_handler():
    """Factory for the Ctrl+X Ctrl+R keystroke handler.

    Separated from the prompt_toolkit binding so it can be unit-tested
    without spinning up a full PromptSession. The key binding (added
    where the KeyBindings instance is constructed) calls this factory's
    return value and submits the result.
    """

    def _handler() -> str:
        return "/reasoning show"

    return _handler
```

Then in the same area as the existing `@kb.add(Keys.ControlX, Keys.ControlE)` chord (around line 209), add:

```python
    @kb.add(Keys.ControlX, Keys.ControlR)
    def _show_last_reasoning(event):
        """Ctrl+X Ctrl+R — inject /reasoning show into the prompt and submit.

        Uses chord syntax (not bare Ctrl+R) so it doesn't stomp
        prompt_toolkit's emacs-mode reverse-search shortcut.
        """
        text = build_reasoning_show_handler()()
        event.current_buffer.text = text
        event.current_buffer.validate_and_handle()
```

- [ ] **Step 5: Run the test**

```bash
pytest tests/test_input_loop_reasoning_keybinding.py -v
```

Expected: PASS.

- [ ] **Step 6: Manual smoke test**

```bash
opencomputer
> what's 2+2?
# wait for response to finalize, then:
# press Ctrl+X then Ctrl+R (chord)
# expected: prompt fills with "/reasoning show" and submits;
#           tree view of last turn appears
```

- [ ] **Step 7: Commit**

```bash
git add opencomputer/cli_ui/input_loop.py tests/test_input_loop_reasoning_keybinding.py
git commit -m "feat(cli_ui): Ctrl+X Ctrl+R chord triggers /reasoning show"
```

---

## Task 9: Update existing E2E test for new collapsed-line format

**Files:**
- Modify: `tests/test_thinking_dropdown_e2e.py`

The existing E2E asserts the old `Thought for X — /reasoning show to expand` format. Update for the new `turn #N · K actions` infix.

- [ ] **Step 1: Read the current assertions**

```bash
grep -n "Thought for\|reasoning show\|show_reasoning" tests/test_thinking_dropdown_e2e.py
```

- [ ] **Step 2: Adjust assertions**

Replace strict-equality string matches like `"Thought for 0.5s — /reasoning show to expand"` with regex matches:

```python
import re

assert re.search(
    r"Thought for [\d.]+m?s.*turn #\d+.*\d+ actions? — /reasoning show to expand",
    captured_output,
), captured_output
```

For tests that don't drive any tool calls, allow the action-count segment to be absent:

```python
assert re.search(
    r"Thought for [\d.]+m?s.*turn #\d+ — /reasoning show to expand",
    captured_output,
), captured_output
```

- [ ] **Step 3: Run the E2E suite**

```bash
pytest tests/test_thinking_dropdown_e2e.py -v
```

Expected: all pass.

- [ ] **Step 4: Run the FULL repo test suite to catch any other format regressions**

```bash
pytest tests/ -x --tb=short
```

Expected: 885+ pass (existing baseline) plus the new tests added in Tasks 1-8. If anything fails: investigate; format coupling outside the dropdown tests is a smell that should be fixed in this PR rather than punted.

- [ ] **Step 5: Lint + typecheck**

```bash
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add tests/test_thinking_dropdown_e2e.py
git commit -m "test(e2e): update collapsed reasoning line assertions for v2 format"
```

---

## Task 10: Documentation + CHANGELOG

**Files:**
- Modify: `OpenComputer/CHANGELOG.md`
- Modify: `OpenComputer/docs/refs/...` only if a relevant slash-command reference doc exists; otherwise skip.

- [ ] **Step 1: Check for an existing slash command reference doc**

```bash
ls OpenComputer/docs/ 2>/dev/null
grep -rln "/reasoning" OpenComputer/docs/ 2>/dev/null | head
```

If a slash-command reference doc exists, update the `/reasoning` entry. If not, skip (not a v1 ship-blocker — `_USAGE` in the command itself is the user-facing reference).

- [ ] **Step 2: Add a CHANGELOG entry**

In `OpenComputer/CHANGELOG.md`, under `[Unreleased]` → `### Added`:

```markdown
- **Reasoning Dropdown v2** — `/reasoning show` now actually expands the most recent thinking block (previously only affected the next turn). New `/reasoning show <N>` and `/reasoning show all` retrieve any past turn from the per-session store. Output renders as a Rich Tree showing the reasoning text and the full sequence of tool actions taken (no longer capped at 3 visible). New `Ctrl+R` keybinding triggers `/reasoning show` without typing. Collapsed line now shows turn id + action count: `💭 Thought for 0.8s · turn #5 · 3 actions — /reasoning show to expand`.
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): reasoning dropdown v2"
```

---

## Task 11: Final integration smoke + push

- [ ] **Step 1: Run the full test suite one more time**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
pytest tests/ -v 2>&1 | tail -20
```

Expected: all pass (no regressions, ~895+ tests total — original 885 plus new ones from Tasks 1, 3, 5, 6, 7, 8).

- [ ] **Step 2: Run a real interactive smoke test**

```bash
opencomputer
> Read pyproject.toml and tell me the version
# After response finalizes, observe:
#   1. Collapsed line: "💭 Thought for X.Xs · turn #1 · 1 action — /reasoning show to expand"
#   2. Type: /reasoning show
#      → tree appears with reasoning + Read action node
#   3. Press Ctrl+R
#      → same tree appears without typing
#   4. > what's the project name?
#   5. After response: /reasoning show 1
#      → first turn's tree (Read action visible)
#   6. /reasoning show all
#      → both turns' trees
```

- [ ] **Step 3: Push branch + open PR**

```bash
git push -u origin feat/reasoning-dropdown-v2
gh pr create --title "feat: reasoning dropdown v2 — retroactive expand + tree view" \
  --body "$(cat <<'EOF'
## Summary
- `/reasoning show` finally works retroactively — expands the most recent thinking block instead of only affecting the next turn (the long-standing UX bug from PR #266).
- New `/reasoning show <N>` and `/reasoning show all` retrieve any past turn.
- Output renders as a Rich Tree: header (turn id + duration + action count) → reasoning text node → one node per tool action with ✓/✗ status.
- Tool actions are now captured unbounded (the visible 3-row panel still evicts; the new `_tool_history` keeps the full sequence for the tree).
- New `Ctrl+R` keybinding triggers `/reasoning show` without typing.
- Collapsed line now shows turn id: `💭 Thought for 0.8s · turn #5 · 3 actions — /reasoning show to expand`.

## Architecture
Per-session `ReasoningStore` (`opencomputer/cli_ui/reasoning_store.py`) holds the last 50 finalized turns. The `StreamingRenderer` accepts the store via `__init__` and pushes a `ReasoningTurn` on `finalize()`. The `/reasoning` slash command reads the store from `runtime.custom["_reasoning_store"]` and renders one or more turns as Rich Trees.

## Test plan
- [x] `tests/test_reasoning_store.py` — 8 unit tests for the store + tree renderer
- [x] `tests/test_streaming_thinking.py` — added 5 tests covering `_tool_history`, push-to-store, collapsed-line format
- [x] `tests/test_reasoning_show_retroactive.py` — 11 tests for slash command behavior
- [x] `tests/test_reasoning_dropdown_integration.py` — full session-renderer-store-command flow
- [x] `tests/test_input_loop_reasoning_keybinding.py` — Ctrl+R handler
- [x] `tests/test_thinking_dropdown_e2e.py` — updated for v2 collapsed-line format
- [x] Manual smoke: collapsed-line shows turn id, /reasoning show prints tree, Ctrl+R triggers same flow

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Checklist (built into the plan; don't skip)

After all tasks complete:

1. **Spec coverage:**
   - User wanted "drop down the user can see whenever he wants" → Tasks 6 (slash command), 8 (Ctrl+X Ctrl+R chord) ✓
   - User wanted tree-style display like Claude.ai web UI → Task 5 (`render_turn_tree`) ✓
   - Tool actions visible in the tree → Tasks 2 (unbounded history), 3 (push), 5 (render) ✓

2. **Type consistency:**
   - `ReasoningStore.append()` signature matches usage in `streaming.py.finalize()` ✓
   - `ToolAction` field names match between renderer push (Task 3) and tree render (Task 5) ✓
   - `runtime.custom["_reasoning_store"]` key spelled identically in cli.py wiring (Task 7) and reasoning_cmd.py (Task 6) ✓
   - `peek_next_id()` (added in Task 4 step 3) is the public surface used by streaming.py — never reach into `_next_id` directly ✓

3. **Backwards compat:**
   - Renderer still works without store (Task 3 test `test_finalize_skips_store_push_when_no_store_attached`) ✓
   - `/reasoning hide`, `/reasoning <level>`, `/reasoning status` still work (Task 6 tests) ✓
   - `runtime.custom["show_reasoning"]` flag still set by `show` (Task 6 test `test_legacy_show_still_sets_flag_for_next_turn`) ✓
   - `tests/test_reasoning_persistence.py` + `test_reasoning_replay_blocks.py` still green (Task 6 step 5) ✓

4. **Edge cases covered by tests:**
   - Empty thinking but tool calls present (Task 3 `test_finalize_records_turn_even_without_thinking`) ✓
   - Thinking present but no tool calls (Task 5 `test_render_turn_tree_handles_no_actions`) ✓
   - Both empty no-op turn — store does NOT record, no noise (Task 3 `test_finalize_skips_empty_no_op_turn`) ✓
   - More than 3 tool calls — history captures all (Task 2 `test_renderer_records_unbounded_tool_history`) ✓
   - Unknown turn id requested (Task 6 `test_show_unknown_turn_id_returns_error`) ✓
   - Store not attached (Task 6 `test_show_with_no_store_returns_helpful_message`) ✓
   - Empty store (Task 6 `test_show_when_store_empty_returns_helpful_message`) ✓
   - Cap at 50 turns — older entries evicted (Task 1 `test_store_caps_to_max_turns`) ✓

5. **Render-pipeline safety:**
   - `_render_to_string` uses `color_system=None` to suppress ANSI escapes that would render as garbage when the dispatcher routes the output as message content via `loop.py:174`. Unicode tree connectors preserved. ✓

6. **No placeholders.** Each step has either real code, a real command, or a real test. No "TBD".

7. **Branch + worktree.** Task 0 creates a dedicated worktree on `feat/reasoning-dropdown-v2`. All code edits happen there; the main worktree stays untouched. ✓

## Audit log — issues caught and resolved before handoff

This plan was self-audited as an expert critic before execution. Issues found and fixed inline:

| # | Issue | Resolution |
|---|---|---|
| 1 | `RuntimeContext` is `frozen=True, slots=True` — would `runtime.custom["_reasoning_store"] = ...` fail? | Verified `custom: dict[str, Any]` is mutable; assignment mutates dict, not field. ✓ |
| 2 | Is `runtime` per-turn or per-session? Per-turn would lose store between turns. | Verified `cli.py:939` constructs ONE per session, reused at line 1116. ✓ |
| 3 | Will ANSI escape codes in `SlashCommandResult.output` render as garbage? | Yes — output flows through `loop.py:174` as message content. Switched `_render_to_string` to `color_system=None`; Unicode connectors only. ✓ |
| 4 | `Ctrl+R` conflicts with prompt_toolkit emacs reverse-search default. | Switched to `Ctrl+X Ctrl+R` chord (matches existing `Ctrl+X Ctrl+E` pattern at `input_loop.py:209`). ✓ |
| 5 | Empty no-op turn would pollute `/reasoning show all`. | Added skip guard in finalize push + `test_finalize_skips_empty_no_op_turn`. ✓ |
| 6 | No worktree per user's hard rule (memory: feedback_worktrees_for_parallel_sessions). | Added Task 0: worktree + branch setup. ✓ |
| 7 | Existing `test_reasoning_persistence.py` + `test_reasoning_replay_blocks.py` could regress on `runtime.custom["show_reasoning"]`. | Added Task 6 step 5: explicit smoke run. ✓ |
| 8 | Plan referenced `kb.add("c-r")` string-style, but file uses `Keys.ControlX, Keys.ControlE` enum-chord style. | Updated Task 8 to use `@kb.add(Keys.ControlX, Keys.ControlR)`. ✓ |
| 9 | `peek_next_id()` reaches into `_next_id` private — coupling. | Added a proper `peek_next_id()` method on `ReasoningStore` in Task 4 + unit test. ✓ |
| 10 | Plan didn't say which slash dispatcher path renders the output. | Verified flow: `agent/slash_commands.py:172` returns `result.output`; `agent/loop.py:174` uses it as message content. ✓ |

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-03-reasoning-dropdown-v2.md`.**

Per the user's explicit sequence: writing-plans → audit → executing-plans. The audit happens BEFORE handoff (next step in this session). After audit refines the plan, this session continues into **Inline Execution** via `superpowers:executing-plans` — batch execution with checkpoints between Tasks for review.
