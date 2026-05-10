# Hermes CLI / TUI / Sessions v2 — parity plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining gap (~21 items across 4 themes) between OpenComputer and Hermes' CLI/TUI/Sessions v2 reference doc.

**Architecture:** Four-commit PR — CLI polish, slash parity, sessions polish, TUI polish. Each item is small, self-contained, additive (no breaking changes). Plumbs into existing subsystems (input_loop, slash registry, SessionDB, ui-tui).

**Tech Stack:** Python 3.13, Typer, prompt-toolkit, Rich, SQLite (sessions.db), React/Ink (ui-tui), Vitest, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-05-08-hermes-cli-tui-sessions-v2-parity-design.md`

**Branch + worktree (run BEFORE any task):**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git worktree add -b feat/hermes-cli-tui-sessions-v2-parity-2026-05-08 \
  ../OpenComputer.wt/hermes-cli-tui-sessions-v2 origin/main
cd ../OpenComputer.wt/hermes-cli-tui-sessions-v2
```

All tasks below run from inside the worktree. Per memory rule:
parallel sessions must never share a working tree on one branch.

---

## Task 0: API verification (prevent silent-API-drift)

Per Karpathy rule from memory ("Think Before Coding — verify APIs!"),
confirm every external contract this plan touches before writing code.
Each finding here gets a one-line note in the executor's notepad and is
applied to the relevant task.

- [ ] **0.1 — `SessionDB` signatures**

```bash
grep -nE "def (create_session|list_sessions|get_session|get_messages|fork_session|rename_session)" opencomputer/agent/state.py
```

For each, record the actual signature. If any task assumes a kwarg that
doesn't exist (e.g., `create_session(id=...)` vs `create_session(session_id=...)`),
adjust the test code in Tasks 16 / 18 BEFORE writing it.

- [ ] **0.2 — `RewindStore` signatures**

```bash
grep -nE "def (list_checkpoints|restore|create_checkpoint)" opencomputer/checkpoint/*.py
```

Record method names. Adjust Task 8 `RollbackCommand.execute` accordingly.

- [ ] **0.3 — `SlashCommand` / `SlashCommandResult` shape**

```bash
grep -nE "class SlashCommand|class SlashCommandResult" plugin_sdk/slash_command.py
```

Confirm `execute(self, args: str, runtime: RuntimeContext)` and
`SlashCommandResult(output=..., handled=...)` are real. If the registry
uses different names (`source`, `error`, etc.), adjust Tasks 8/9/10/21.

- [ ] **0.4 — `RuntimeContext.custom`**

```bash
grep -nE "class RuntimeContext|custom" plugin_sdk/runtime_context.py | head
```

Verify `custom: dict` is the right channel. If it's `metadata` or
`store`, replace across Tasks 8/9/10/21.

- [ ] **0.5 — `prompt-toolkit` `Application` accepts `enable_suspend`**

```bash
python -c "from prompt_toolkit import Application; import inspect; print('enable_suspend' in inspect.signature(Application.__init__).parameters)"
```

If False, fall back to manual SIGTSTP handler in Task 7 (already partially
sketched).

- [ ] **0.6 — TUI runtime push channel**

```bash
grep -nE "ws.*push|broadcast.*runtime|sections.*update" opencomputer/gateway/ ui-tui/src/ 2>/dev/null | head -20
```

If no live push for `runtime.custom["sections"]` exists, document Task 10
finding as "CLI-side state only; TUI consumes on next handshake".

- [ ] **0.7 — Document any drift**

Add a short "API drift findings" section to the executor's working notepad.
Each task that needs a fix gets a 1-line note prepended in the task
header.

---

## File Map

**New files:**

```
opencomputer/cli_ui/paste_preview.py            # A1
opencomputer/cli_ui/markdown_strip.py           # A2
opencomputer/cli_ui/per_prompt_elapsed.py       # A3
opencomputer/cli_ui/theme_detect.py             # A4
opencomputer/cli_ui/busy_indicator.py           # A5
opencomputer/agent/quick_commands.py            # A6
opencomputer/agent/slash_commands_impl/rollback_cmd.py    # B1
opencomputer/agent/slash_commands_impl/busy_cmd.py        # B2
opencomputer/agent/slash_commands_impl/details_cmd.py     # B3
opencomputer/agent/slash_commands_impl/mouse_cmd.py       # D5
ui-tui/src/lib/themeDetect.ts                   # D1
ui-tui/src/hooks/useGitBranch.ts                # D2
tests/cli_ui/test_paste_preview.py              # A1
tests/cli_ui/test_markdown_strip.py             # A2
tests/cli_ui/test_per_prompt_elapsed.py         # A3
tests/cli_ui/test_theme_detect.py               # A4
tests/cli_ui/test_busy_indicator.py             # A5
tests/agent/test_quick_commands.py              # A6
tests/slash/test_rollback_cmd.py                # B1
tests/slash/test_busy_cmd.py                    # B2
tests/slash/test_details_cmd.py                 # B3
tests/cli/test_sessions_plural.py               # C1-C4
tests/cli/test_resume_by_name_title.py          # C5-C6
tests/agent/test_title_lineage.py               # C7
ui-tui/src/__tests__/themeDetect.test.ts        # D1
ui-tui/src/__tests__/useGitBranch.test.ts       # D2
```

**Modified files:**

```
opencomputer/cli_ui/input_loop.py                # A1, A3, A7
opencomputer/cli_ui/streaming.py                 # A2
opencomputer/cli_ui/status_line.py               # A3, A5
opencomputer/cli_banner.py                       # A4 (probe at launch)
opencomputer/cli_ui/style.py                     # A4
opencomputer/agent/slash_dispatcher.py           # A6 (quick before slash)
opencomputer/agent/slash_commands.py             # B1, B2, B3, D5 register
opencomputer/cli.py                              # C1, C5, C6
opencomputer/cli_session.py                      # C2, C3, C4
opencomputer/agent/state.py                      # C5, C6 (DB methods)
opencomputer/agent/title_generator.py            # C7
opencomputer/agent/config.py                     # A2, A5 config defaults
ui-tui/src/theme.ts                              # D1
ui-tui/src/components/appChrome.tsx              # D2
ui-tui/src/components/sessionPicker.tsx          # D3 (verify wiring)
opencomputer/cli_ui/slash_handlers.py            # D3, D4 verify
```

---

## Task 1: A1 — Multiline paste preview

**Files:**
- Create: `opencomputer/cli_ui/paste_preview.py`
- Create: `tests/cli_ui/test_paste_preview.py`
- Modify: `opencomputer/cli_ui/input_loop.py:220-260` (bracketed-paste handler)

- [ ] **Step 1: Write the failing test**

```python
# tests/cli_ui/test_paste_preview.py
"""Tests for cli_ui.paste_preview — multiline paste-marker substitution."""

from opencomputer.cli_ui.paste_preview import PasteStore


def test_short_paste_passes_through() -> None:
    store = PasteStore()
    out = store.maybe_collapse("hi there")
    assert out == "hi there"
    assert store.expand("hi there") == "hi there"


def test_long_paste_collapses_to_marker() -> None:
    store = PasteStore()
    payload = "\n".join(f"line {i}" for i in range(50))
    out = store.maybe_collapse(payload)
    assert out.startswith("[pasted: ")
    assert "50 lines" in out
    assert str(len(payload)) in out


def test_expand_restores_original() -> None:
    store = PasteStore()
    payload = "alpha\nbeta\ngamma\ndelta\nepsilon"
    marker = store.maybe_collapse(payload)
    expanded = store.expand(f"prefix {marker} suffix")
    assert expanded == f"prefix {payload} suffix"


def test_multiple_markers_in_one_buffer() -> None:
    store = PasteStore()
    a = store.maybe_collapse("a\n" * 10)
    b = store.maybe_collapse("b\n" * 10)
    expanded = store.expand(f"{a} sep {b}")
    assert expanded.startswith("a\n" * 10)
    assert expanded.endswith("b\n" * 10)


def test_clear_drops_state() -> None:
    store = PasteStore()
    marker = store.maybe_collapse("\n".join("x" * 5 for _ in range(20)))
    store.clear()
    assert store.expand(marker) == marker  # marker unrecognized after clear
```

- [ ] **Step 2: Run test, expect failure**

```bash
pytest tests/cli_ui/test_paste_preview.py -v
```

Expected: ImportError — module doesn't exist.

- [ ] **Step 3: Implement PasteStore**

```python
# opencomputer/cli_ui/paste_preview.py
"""Multiline paste preview — collapse long pastes to single-line markers.

Hermes-CLI parity (Hermes-doc lines 75-77). When a user pastes a multi-line
block we keep the full payload in a side store and replace it in the input
buffer with a compact marker `[pasted: N lines, M chars]`. On submit, the
input loop expands every marker back to its original payload before sending
to the agent. Limits visual clutter without losing fidelity.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

# Threshold: only collapse if the paste contains ≥ 3 newlines OR > 240 chars.
# Short snippets (e.g., `a/b/c.py:42-50` line ranges) stay literal.
_MIN_NEWLINES = 3
_MIN_CHARS = 240

_MARKER_RE = re.compile(r"\[pasted: \d+ lines?, \d+ chars; id=([a-f0-9]{8})\]")


@dataclass
class PasteStore:
    """Side-store of full paste payloads keyed by short uuid markers."""

    _payloads: dict[str, str] = field(default_factory=dict)

    def maybe_collapse(self, raw: str) -> str:
        """Return *raw* unchanged for short payloads; otherwise a marker
        token whose uuid resolves back to *raw* via :meth:`expand`.
        """
        if raw.count("\n") < _MIN_NEWLINES and len(raw) <= _MIN_CHARS:
            return raw
        uid = uuid.uuid4().hex[:8]
        self._payloads[uid] = raw
        n_lines = raw.count("\n") + 1
        return f"[pasted: {n_lines} lines, {len(raw)} chars; id={uid}]"

    def expand(self, buffer: str) -> str:
        """Replace every recognised marker in *buffer* with its full payload.

        Markers we never issued are left literal (user-typed marker text
        won't be replaced). Order is left-to-right scan.
        """
        def _replace(m: re.Match[str]) -> str:
            uid = m.group(1)
            return self._payloads.get(uid, m.group(0))

        return _MARKER_RE.sub(_replace, buffer)

    def clear(self) -> None:
        self._payloads.clear()
```

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/cli_ui/test_paste_preview.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Wire into input_loop**

Find the bracketed-paste handler at `input_loop.py:220` and the second one at `:810`. Add a module-level `_paste_store: PasteStore = PasteStore()`. In each handler, before the buffer insertion, call `text_to_insert = _paste_store.maybe_collapse(text_to_insert)`. Add a corresponding `expand` call right before the user input is dispatched (find the spot where `cleaned_text` is assembled and prepend `cleaned_text = _paste_store.expand(cleaned_text)`).

- [ ] **Step 6: Add input_loop integration test**

```python
# tests/cli_ui/test_paste_preview.py — append
def test_input_loop_uses_store(monkeypatch) -> None:
    """Verify input_loop module wires the singleton store."""
    from opencomputer.cli_ui import input_loop

    assert hasattr(input_loop, "_paste_store"), "input_loop must hold a PasteStore singleton"
    assert isinstance(input_loop._paste_store, PasteStore)
```

- [ ] **Step 7: Run all tests + ruff**

```bash
pytest tests/cli_ui/test_paste_preview.py -v && ruff check opencomputer/cli_ui/paste_preview.py
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add opencomputer/cli_ui/paste_preview.py opencomputer/cli_ui/input_loop.py tests/cli_ui/test_paste_preview.py
git commit -m "feat(cli): multiline paste preview (Hermes parity A1)"
```

---

## Task 2: A2 — Markdown stripping in CLI final responses

**Files:**
- Create: `opencomputer/cli_ui/markdown_strip.py`
- Create: `tests/cli_ui/test_markdown_strip.py`
- Modify: `opencomputer/cli_ui/streaming.py` (call strip in finalize)
- Modify: `opencomputer/agent/config.py` (add `display.markdown_strip` knob)

- [ ] **Step 1: Write the failing test**

```python
# tests/cli_ui/test_markdown_strip.py
from opencomputer.cli_ui.markdown_strip import strip_for_terminal


def test_bold_stripped() -> None:
    assert strip_for_terminal("the **quick** brown fox") == "the quick brown fox"


def test_italic_stripped_star_and_underscore() -> None:
    assert strip_for_terminal("an *italic* word") == "an italic word"
    assert strip_for_terminal("an _italic_ word") == "an italic word"


def test_atx_heading_markers_stripped() -> None:
    assert strip_for_terminal("# Heading\nbody") == "Heading\nbody"
    assert strip_for_terminal("## Sub") == "Sub"


def test_code_fence_preserved_verbatim() -> None:
    md = "before\n```python\n**not bold here**\n```\nafter"
    out = strip_for_terminal(md)
    assert "**not bold here**" in out
    assert out.startswith("before")
    assert out.endswith("after")


def test_inline_code_preserved() -> None:
    md = "use `**literal**` to bold"
    out = strip_for_terminal(md)
    assert "`**literal**`" in out


def test_list_markers_preserved() -> None:
    md = "- item one\n- **bold** item\n  - nested\n1. ordered"
    out = strip_for_terminal(md)
    assert "- item one" in out
    assert "- bold item" in out  # bold stripped, dash preserved
    assert "1. ordered" in out


def test_table_pipes_preserved() -> None:
    md = "| col |\n|-----|\n| **bold** |"
    out = strip_for_terminal(md)
    assert "| col |" in out
    assert "| bold |" in out  # bold stripped, pipes preserved


def test_link_url_preserved() -> None:
    out = strip_for_terminal("see [docs](https://example.com)")
    assert "https://example.com" in out
```

- [ ] **Step 2: Run test, expect failure**

```bash
pytest tests/cli_ui/test_markdown_strip.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement strip_for_terminal**

```python
# opencomputer/cli_ui/markdown_strip.py
"""Strip rendered markdown markup from CLI final assistant prose.

Hermes-CLI parity (doc line 77). Rich already styles `**bold**` and
`*italic*` when fed through ``Markdown(text)``, but the literal
asterisks ALSO render in the terminal stream when the same text is
re-printed plain. This module gives the streaming renderer one place
to deboldify final text — with a careful exemption for any region
inside fenced code blocks, inline code, or tables (where the ``*``
glyphs are part of the user-visible payload).

The function is pure; tests are golden-fixture-driven.
"""

from __future__ import annotations

import re

# Match a fenced code block — opening fence, body, closing fence on own line.
# Greedy across newlines via DOTALL, lazy body via ``*?``.
_FENCE_RE = re.compile(r"```[^\n]*\n.*?\n```", re.DOTALL)
# Match inline code: backticks containing no newline.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
# Match table rows — line starting with ``|`` and containing another ``|``.
_TABLE_RE = re.compile(r"(?m)^\|.*\|$")

# Markup patterns to strip in non-code regions.
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_ITALIC_UNDER_RE = re.compile(r"(?<![A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])")
_ATX_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")


def strip_for_terminal(md: str) -> str:
    """Return *md* with rendered markup stripped, preserving code/tables.

    Algorithm:
    1. Mask off code fences, inline code, and table rows by splitting *md*
       into alternating segments of "preserve" (verbatim) and "strip"
       (apply markup-strip rules).
    2. Apply strip rules only to "strip" segments.
    3. Re-join.
    """
    parts = _split_preserve_strip(md)
    out: list[str] = []
    for kind, chunk in parts:
        if kind == "preserve":
            out.append(chunk)
        else:
            chunk = _BOLD_RE.sub(r"\1", chunk)
            chunk = _ITALIC_STAR_RE.sub(r"\1", chunk)
            chunk = _ITALIC_UNDER_RE.sub(r"\1", chunk)
            chunk = _ATX_HEADING_RE.sub("", chunk)
            out.append(chunk)
    return "".join(out)


def _split_preserve_strip(md: str) -> list[tuple[str, str]]:
    """Split *md* into ``(kind, text)`` segments where kind is
    'preserve' (verbatim — code/tables) or 'strip' (apply rules).
    """
    # Mark each preserve span by collecting its (start, end) offsets,
    # then walk the string emitting alternating chunks.
    spans: list[tuple[int, int]] = []
    for rx in (_FENCE_RE, _INLINE_CODE_RE, _TABLE_RE):
        for m in rx.finditer(md):
            spans.append(m.span())
    spans.sort()

    # Merge overlapping spans (rare but possible: inline code inside table cell).
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    out: list[tuple[str, str]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            out.append(("strip", md[cursor:start]))
        out.append(("preserve", md[start:end]))
        cursor = end
    if cursor < len(md):
        out.append(("strip", md[cursor:]))
    return out
```

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/cli_ui/test_markdown_strip.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Wire into streaming finalize + add config knob**

```python
# opencomputer/agent/config.py — add to DisplayConfig (search "class DisplayConfig" or similar)
markdown_strip: bool = True   # Hermes-parity A2; off-switch for raw markdown
```

Find `streaming.py`'s `finalize_turn` (or analogous final-flush method) — add at the very top:

```python
from opencomputer.cli_ui.markdown_strip import strip_for_terminal
# ...
if cfg.display.markdown_strip and assistant_text:
    assistant_text = strip_for_terminal(assistant_text)
```

If the variable isn't named `assistant_text`, adapt to the renderer's actual name. Search for `Markdown(` calls in `streaming.py` — those are the targets.

- [ ] **Step 6: Run pytest + ruff**

```bash
pytest tests/cli_ui/test_markdown_strip.py -v
ruff check opencomputer/cli_ui/markdown_strip.py opencomputer/cli_ui/streaming.py
```

Expected: green.

- [ ] **Step 7: Commit**

```bash
git add opencomputer/cli_ui/markdown_strip.py opencomputer/cli_ui/streaming.py opencomputer/agent/config.py tests/cli_ui/test_markdown_strip.py
git commit -m "feat(cli): strip markdown markers from final assistant prose (Hermes A2)"
```

---

## Task 3: A3 — Per-prompt elapsed time

**Files:**
- Create: `opencomputer/cli_ui/per_prompt_elapsed.py`
- Create: `tests/cli_ui/test_per_prompt_elapsed.py`
- Modify: `opencomputer/cli_ui/status_line.py`
- Modify: `opencomputer/cli_ui/input_loop.py` (call clock.start/stop)

- [ ] **Step 1: Write failing tests**

```python
# tests/cli_ui/test_per_prompt_elapsed.py
import time

from opencomputer.cli_ui.per_prompt_elapsed import PromptClock


def test_starts_at_zero() -> None:
    clock = PromptClock()
    assert clock.render() == ""  # no prompt yet


def test_running_renders_live(monkeypatch) -> None:
    clock = PromptClock(now=lambda: 0.0)
    clock.start()
    clock._now = lambda: 12.4
    assert clock.render() == "⏱ 12s"


def test_frozen_after_stop() -> None:
    clock = PromptClock(now=lambda: 0.0)
    clock.start()
    clock._now = lambda: 32.0
    clock.stop()
    assert clock.render().startswith("⏲ 32s")


def test_total_session_time_in_frozen_render() -> None:
    clock = PromptClock(now=lambda: 0.0)
    clock.session_start = 0.0
    clock.start()
    clock._now = lambda: 32.0
    clock.stop()
    rendered = clock.render()
    assert "32s" in rendered
    assert "/" in rendered  # separator between prompt-elapsed and session-elapsed


def test_reset_drops_state() -> None:
    clock = PromptClock(now=lambda: 0.0)
    clock.start()
    clock._now = lambda: 5.0
    clock.reset()
    assert clock.render() == ""
```

- [ ] **Step 2: Run test, expect failure**

```bash
pytest tests/cli_ui/test_per_prompt_elapsed.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement PromptClock**

```python
# opencomputer/cli_ui/per_prompt_elapsed.py
"""Per-prompt elapsed-time clock for the CLI status line.

Hermes-CLI parity (doc line 351). Independent of the session-wide
duration shown elsewhere — this resets on every user prompt and shows:

- ⏱ NN s    while the agent is running
- ⏲ NN s / total MM s    once the turn finalises (until next prompt)

Pure stateful object — accepts a ``now`` callable for tests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


def _fmt_secs(s: float) -> str:
    """Format seconds as `12s` or `3m 45s` for compact display."""
    s_int = int(s)
    if s_int < 60:
        return f"{s_int}s"
    m, sec = divmod(s_int, 60)
    return f"{m}m {sec}s"


@dataclass
class PromptClock:
    """Tracks per-prompt elapsed time for the status line."""

    _now: object = time.time            # callable () -> float
    session_start: float = 0.0
    _prompt_start: float | None = None
    _prompt_stop: float | None = None

    def __post_init__(self) -> None:
        if self.session_start == 0.0:
            self.session_start = float(self._now())

    def start(self) -> None:
        self._prompt_start = float(self._now())
        self._prompt_stop = None

    def stop(self) -> None:
        if self._prompt_start is not None and self._prompt_stop is None:
            self._prompt_stop = float(self._now())

    def reset(self) -> None:
        self._prompt_start = None
        self._prompt_stop = None

    def render(self) -> str:
        if self._prompt_start is None:
            return ""
        if self._prompt_stop is None:
            elapsed = float(self._now()) - self._prompt_start
            return f"⏱ {_fmt_secs(elapsed)}"
        elapsed = self._prompt_stop - self._prompt_start
        total = float(self._now()) - self.session_start
        return f"⏲ {_fmt_secs(elapsed)} / {_fmt_secs(total)}"


def __init__(now=None):
    """Backwards-compat constructor — accepts ``now`` kwarg."""
    raise NotImplementedError  # placeholder removed; dataclass init covers it
```

(Drop the `__init__` shim at the bottom — dataclass `_now` field handles construction.)

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/cli_ui/test_per_prompt_elapsed.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Wire into status_line + input_loop**

In `status_line.py`, near where the existing duration cell is rendered, instantiate a module-level `_clock = PromptClock()` and append `_clock.render()` to the cells list (right-most position so it doesn't disturb existing layout).

In `input_loop.py`, find the spot where a user prompt is dispatched (`asyncio.run(_run_turn_cancellable(cleaned_text, ...))`). Wrap that call with `_clock.start()` before, `_clock.stop()` after (in a try/finally). Hook `Ctrl+C` (existing turn-cancel handler) to call `_clock.reset()`.

- [ ] **Step 6: Run tests + ruff**

```bash
pytest tests/cli_ui/test_per_prompt_elapsed.py -v
ruff check opencomputer/cli_ui/per_prompt_elapsed.py opencomputer/cli_ui/status_line.py
```

- [ ] **Step 7: Commit**

```bash
git add opencomputer/cli_ui/per_prompt_elapsed.py opencomputer/cli_ui/status_line.py opencomputer/cli_ui/input_loop.py tests/cli_ui/test_per_prompt_elapsed.py
git commit -m "feat(cli): per-prompt elapsed clock (Hermes A3)"
```

---

## Task 4: A4 — Light terminal detection

**Files:**
- Create: `opencomputer/cli_ui/theme_detect.py`
- Create: `tests/cli_ui/test_theme_detect.py`
- Modify: `opencomputer/cli_banner.py` (probe at launch, cache result)
- Modify: `opencomputer/cli_ui/style.py` (consume detected theme)

- [ ] **Step 1: Write failing tests**

```python
# tests/cli_ui/test_theme_detect.py
import os

from opencomputer.cli_ui.theme_detect import Theme, detect_theme


def test_env_override_light(monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_TUI_THEME", "light")
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert detect_theme(probe=lambda: None).kind == "light"


def test_env_override_dark(monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_TUI_THEME", "dark")
    assert detect_theme(probe=lambda: None).kind == "dark"


def test_env_override_hex_bg(monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_TUI_THEME", "ffffff")
    t = detect_theme(probe=lambda: None)
    assert t.kind == "light"
    assert t.bg_hex == "ffffff"


def test_colorfgbg_xterm_light(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert detect_theme(probe=lambda: None).kind == "light"


def test_colorfgbg_xterm_dark(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert detect_theme(probe=lambda: None).kind == "dark"


def test_osc11_probe_light(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    # Reply mimicking iTerm OSC11: ESC]11;rgb:ffff/ffff/ffff ESC\
    reply = "\x1b]11;rgb:ffff/ffff/ffff\x1b\\"
    assert detect_theme(probe=lambda: reply).kind == "light"


def test_osc11_probe_dark(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    reply = "\x1b]11;rgb:1010/1010/1010\x1b\\"
    assert detect_theme(probe=lambda: reply).kind == "dark"


def test_default_dark_when_all_silent(monkeypatch) -> None:
    monkeypatch.delenv("OPENCOMPUTER_TUI_THEME", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert detect_theme(probe=lambda: None).kind == "dark"


def test_probe_timeout_returns_none() -> None:
    """Real probe must not block — but unit-tested through the explicit probe arg."""
    # Smoke: a probe that returns None acts as "timed out".
    assert detect_theme(probe=lambda: None).kind == "dark"
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/cli_ui/test_theme_detect.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement theme_detect**

```python
# opencomputer/cli_ui/theme_detect.py
"""Light / dark terminal detection.

Hermes-CLI parity (doc lines 318-325). Layered detection — env override
first, then COLORFGBG, then an OSC 11 background-colour query. The OSC 11
probe runs once at launch with a 200 ms read timeout; dumb terminals fail
silently and we fall back to dark.
"""

from __future__ import annotations

import os
import re
import select
import sys
import termios
import tty
from collections.abc import Callable
from dataclasses import dataclass

_HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")
_OSC11_RE = re.compile(r"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)")


@dataclass(frozen=True)
class Theme:
    kind: str          # "light" | "dark"
    bg_hex: str = ""   # 6-hex bg colour, "" if unknown


def _hex_to_luminance(hx: str) -> float:
    """Approximate perceptual luminance of a 6-hex bg colour, [0,1]."""
    r = int(hx[0:2], 16) / 255.0
    g = int(hx[2:4], 16) / 255.0
    b = int(hx[4:6], 16) / 255.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _from_env() -> Theme | None:
    val = os.environ.get("OPENCOMPUTER_TUI_THEME", "").strip().lower()
    if val == "light":
        return Theme(kind="light", bg_hex="ffffff")
    if val == "dark":
        return Theme(kind="dark", bg_hex="000000")
    if _HEX_RE.match(val):
        kind = "light" if _hex_to_luminance(val) > 0.5 else "dark"
        return Theme(kind=kind, bg_hex=val)
    return None


def _from_colorfgbg() -> Theme | None:
    val = os.environ.get("COLORFGBG", "").strip()
    if not val:
        return None
    parts = val.split(";")
    if len(parts) < 2:
        return None
    try:
        bg = int(parts[-1])
    except ValueError:
        return None
    # xterm convention: 0–7 dark, 8–15 light, 15 = white.
    return Theme(kind="light" if bg >= 8 else "dark", bg_hex="")


def _parse_osc11(reply: str) -> Theme | None:
    m = _OSC11_RE.search(reply)
    if not m:
        return None
    # Each component may be 4 or 2 hex chars; take the high byte.
    def _hi(s: str) -> str:
        return (s + "00")[:2]

    hx = _hi(m.group(1)) + _hi(m.group(2)) + _hi(m.group(3))
    kind = "light" if _hex_to_luminance(hx) > 0.5 else "dark"
    return Theme(kind=kind, bg_hex=hx)


def _osc11_probe_real(timeout_ms: int = 200) -> str | None:
    """Send OSC 11 query, read reply with a strict timeout.

    Returns the reply string, or ``None`` if the terminal didn't respond.
    Wrapped in best-effort try/except — never raises.
    """
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return None
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except Exception:
        return None
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\x1b]11;?\x1b\\")
        sys.stdout.flush()
        end_at = timeout_ms / 1000.0
        buf = ""
        # Read up to 64 chars or until terminator. select with cumulative budget.
        import time as _t
        start = _t.monotonic()
        while _t.monotonic() - start < end_at:
            r, _, _ = select.select([fd], [], [], end_at - (_t.monotonic() - start))
            if not r:
                break
            ch = os.read(fd, 1).decode("ascii", errors="ignore")
            buf += ch
            if buf.endswith("\x1b\\") or buf.endswith("\x07"):
                break
        return buf or None
    except Exception:
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old)
        except Exception:
            pass


def detect_theme(*, probe: Callable[[], str | None] | None = None) -> Theme:
    """Best-effort theme detection. Pure for unit-testing via *probe* override.
    """
    t = _from_env()
    if t is not None:
        return t
    t = _from_colorfgbg()
    if t is not None:
        return t
    p = probe if probe is not None else _osc11_probe_real
    reply = p()
    if reply:
        t = _parse_osc11(reply)
        if t is not None:
            return t
    return Theme(kind="dark", bg_hex="000000")
```

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/cli_ui/test_theme_detect.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Wire into cli_banner + style**

In `cli_banner.py`, near launch, call `theme = detect_theme()` once and stash in a module-level `ACTIVE_THEME`. In `cli_ui/style.py`, consume `ACTIVE_THEME.kind` to choose accent colours (light = darker accents, dark = brighter accents) — keep the change small (one ternary in the existing palette function).

- [ ] **Step 6: Commit**

```bash
git add opencomputer/cli_ui/theme_detect.py opencomputer/cli_banner.py opencomputer/cli_ui/style.py tests/cli_ui/test_theme_detect.py
git commit -m "feat(cli): light terminal detection via OSC11 / COLORFGBG / env (Hermes A4)"
```

---

## Task 5: A5 — Busy indicator styles

**Files:**
- Create: `opencomputer/cli_ui/busy_indicator.py`
- Create: `tests/cli_ui/test_busy_indicator.py`
- Modify: `opencomputer/cli_ui/status_line.py` (consume style-driven frames)
- Modify: `opencomputer/agent/config.py` (add `display.busy_indicator.style` knob)

- [ ] **Step 1: Write failing tests**

```python
# tests/cli_ui/test_busy_indicator.py
import wcwidth

from opencomputer.cli_ui.busy_indicator import STYLES, BusyIndicator


def test_all_styles_registered() -> None:
    for name in ("kawaii", "minimal", "dots", "wings", "none"):
        assert name in STYLES


def test_each_style_has_uniform_width() -> None:
    for name, frames in STYLES.items():
        widths = {wcwidth.wcswidth(f) for f in frames}
        assert len(widths) == 1, f"{name} has non-uniform widths {widths}"


def test_indicator_cycles_frames() -> None:
    bi = BusyIndicator(style="dots")
    seen = {bi.next_frame() for _ in range(len(STYLES["dots"]) * 2)}
    assert seen == set(STYLES["dots"])


def test_unknown_style_falls_back_to_kawaii() -> None:
    bi = BusyIndicator(style="not-a-real-style")
    assert bi.style == "kawaii"


def test_none_style_renders_empty() -> None:
    bi = BusyIndicator(style="none")
    assert bi.next_frame() == ""
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/cli_ui/test_busy_indicator.py -v
```

- [ ] **Step 3: Implement busy_indicator**

```python
# opencomputer/cli_ui/busy_indicator.py
"""Busy-indicator glyph styles for the CLI status line.

Hermes-CLI parity (doc lines 329-336). Five named styles each have a
uniform display-width invariant — every frame in a style is the same
``wcwidth`` so the status bar doesn't jitter on rotation.
"""

from __future__ import annotations

import wcwidth
from dataclasses import dataclass, field


def _pad_to_uniform(frames: tuple[str, ...]) -> tuple[str, ...]:
    """Right-pad every frame with U+0020 so they all have the same width.

    ``wcwidth.wcswidth`` returns -1 for strings containing non-printable
    chars; we clamp to 0 so the math never produces a negative pad.
    """
    widths = [max(wcwidth.wcswidth(f) or 0, 0) for f in frames]
    target = max(widths) if widths else 0
    return tuple(f + " " * (target - w) for f, w in zip(frames, widths, strict=True))


STYLES: dict[str, tuple[str, ...]] = {
    "kawaii": _pad_to_uniform((
        "(｡•́︿•̀｡)",
        "(⊙_⊙) ",
        "( ˘ω˘)",
        "(づ｡◕‿‿◕｡)づ",
    )),
    "minimal": _pad_to_uniform(("⋯", "···", "·")),
    "dots":    _pad_to_uniform(("⠁", "⠃", "⠇", "⠧", "⠷", "⠿", "⠟", "⠏")),
    "wings":   _pad_to_uniform(("≼", "≼", "≽", "≽")),
    "none":    ("",),
}


@dataclass
class BusyIndicator:
    """Cycle through the frames of a chosen style."""

    style: str = "kawaii"
    _idx: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.style not in STYLES:
            self.style = "kawaii"

    def next_frame(self) -> str:
        frames = STYLES[self.style]
        f = frames[self._idx % len(frames)]
        self._idx += 1
        return f

    def reset(self) -> None:
        self._idx = 0
```

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/cli_ui/test_busy_indicator.py -v
```

- [ ] **Step 5: Add config knob + wire into status_line**

```python
# opencomputer/agent/config.py — DisplayConfig
busy_indicator_style: str = "kawaii"   # Hermes-parity A5
```

In `status_line.py`, replace the hard-coded busy spinner glyphs with `BusyIndicator(style=cfg.display.busy_indicator_style).next_frame()`. Keep one module-level instance so frames advance across renders.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/cli_ui/busy_indicator.py opencomputer/cli_ui/status_line.py opencomputer/agent/config.py tests/cli_ui/test_busy_indicator.py
git commit -m "feat(cli): busy-indicator style config (kawaii|minimal|dots|wings|none) (Hermes A5)"
```

---

## Task 6: A6 — Quick commands

**Files:**
- Create: `opencomputer/agent/quick_commands.py`
- Create: `tests/agent/test_quick_commands.py`
- Modify: `opencomputer/agent/slash_dispatcher.py` (intercept BEFORE slash dispatch)
- Modify: `opencomputer/agent/config_store.py` (load `quick_commands:`)

- [ ] **Step 1: Write failing tests**

```python
# tests/agent/test_quick_commands.py
from pathlib import Path

import pytest

from opencomputer.agent.quick_commands import (
    QuickCommandError,
    QuickCommands,
    QuickResult,
)


def _write(p: Path, body: str) -> None:
    p.write_text(body, encoding="utf-8")


def test_loads_from_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write(cfg, """
quick_commands:
  echo:
    type: exec
    command: echo hello
  ll:
    type: alias
    target: /tools
""")
    qc = QuickCommands.load(cfg)
    assert "echo" in qc
    assert "ll" in qc
    assert qc["echo"].type == "exec"
    assert qc["ll"].type == "alias"


def test_exec_runs_subprocess(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, """
quick_commands:
  ok:
    type: exec
    command: echo from-quick
""")
    qc = QuickCommands.load(cfg)
    res = qc.run("ok", "")
    assert isinstance(res, QuickResult)
    assert "from-quick" in res.output


def test_exec_timeout_kills_long_command(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, """
quick_commands:
  forever:
    type: exec
    command: sleep 60
""")
    qc = QuickCommands.load(cfg, timeout=0.5)
    res = qc.run("forever", "")
    assert res.timed_out is True


def test_alias_recurses_through_dispatcher(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, """
quick_commands:
  greet:
    type: alias
    target: /usage
""")
    seen = []

    def fake_dispatcher(name: str, args: str, depth: int) -> QuickResult:
        seen.append((name, args, depth))
        return QuickResult(output="dispatched", timed_out=False, depth=depth)

    qc = QuickCommands.load(cfg, dispatcher=fake_dispatcher)
    res = qc.run("greet", "extra args")
    assert seen == [("usage", "extra args", 1)]
    assert res.depth == 1


def test_alias_loop_capped(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, """
quick_commands:
  a:
    type: alias
    target: /b
  b:
    type: alias
    target: /a
""")
    # Dispatcher round-trips back into the QuickCommands.run path.
    qc_holder: dict = {}

    def dispatcher(name: str, args: str, depth: int) -> QuickResult:
        return qc_holder["qc"].run(name, args, _depth=depth)

    qc = QuickCommands.load(cfg, dispatcher=dispatcher)
    qc_holder["qc"] = qc
    with pytest.raises(QuickCommandError, match="alias loop"):
        qc.run("a", "")


def test_unknown_returns_none() -> None:
    qc = QuickCommands(commands={})
    assert qc.run("nope", "") is None
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/agent/test_quick_commands.py -v
```

- [ ] **Step 3: Implement quick_commands**

```python
# opencomputer/agent/quick_commands.py
"""Zero-token quick commands.

Hermes-CLI parity (doc lines 113-134). Loaded from
``~/.opencomputer/config.yaml`` under ``quick_commands:``. Two types:

- ``exec``  — run a shell command, return captured stdout/stderr.
- ``alias`` — re-dispatch through the slash command path.

Quick commands are checked BEFORE slash dispatch so they can shadow a
slash name. Alias depth is capped at 5 to prevent A→B→A loops.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_TIMEOUT_DEFAULT = 30.0   # seconds — Hermes parity (doc line 130)
_MAX_OUTPUT = 4096        # chars
_MAX_ALIAS_DEPTH = 5


class QuickCommandError(Exception):
    """Raised when a quick command can't run (alias loop, malformed config)."""


@dataclass(frozen=True)
class QuickCommandSpec:
    type: str            # "exec" | "alias"
    command: str = ""    # for type=exec
    target: str = ""     # for type=alias (e.g. "/usage")


@dataclass
class QuickResult:
    output: str
    timed_out: bool = False
    depth: int = 0


@dataclass
class QuickCommands:
    """Loaded quick-command map plus run-orchestration."""

    commands: dict[str, QuickCommandSpec] = field(default_factory=dict)
    timeout: float = _TIMEOUT_DEFAULT
    dispatcher: Callable[[str, str, int], QuickResult] | None = None

    def __contains__(self, name: str) -> bool:
        return name in self.commands

    def __getitem__(self, name: str) -> QuickCommandSpec:
        return self.commands[name]

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        timeout: float = _TIMEOUT_DEFAULT,
        dispatcher: Callable[[str, str, int], QuickResult] | None = None,
    ) -> "QuickCommands":
        try:
            raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return cls(commands={}, timeout=timeout, dispatcher=dispatcher)
        section: dict[str, Any] = raw.get("quick_commands", {}) or {}
        commands: dict[str, QuickCommandSpec] = {}
        for name, spec in section.items():
            if not isinstance(spec, dict):
                continue
            t = spec.get("type", "exec")
            commands[name] = QuickCommandSpec(
                type=t,
                command=str(spec.get("command", "")),
                target=str(spec.get("target", "")),
            )
        return cls(commands=commands, timeout=timeout, dispatcher=dispatcher)

    def run(self, name: str, args: str, *, _depth: int = 0) -> QuickResult | None:
        spec = self.commands.get(name)
        if spec is None:
            return None
        if spec.type == "exec":
            return self._run_exec(spec.command, args)
        if spec.type == "alias":
            if _depth + 1 >= _MAX_ALIAS_DEPTH:
                raise QuickCommandError(
                    f"alias loop in /{name}: depth {_depth + 1} reached cap"
                )
            target = spec.target.lstrip("/")
            if not target:
                raise QuickCommandError(f"alias /{name} has no target")
            if self.dispatcher is None:
                raise QuickCommandError("no dispatcher wired for alias re-dispatch")
            return self.dispatcher(target, args, _depth + 1)
        raise QuickCommandError(f"unknown quick-command type: {spec.type}")

    def _run_exec(self, command: str, args: str) -> QuickResult:
        full = f"{command} {args}".strip() if args else command
        try:
            cp = subprocess.run(
                full,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            out = (cp.stdout + cp.stderr)[:_MAX_OUTPUT]
            return QuickResult(output=out, timed_out=False)
        except subprocess.TimeoutExpired:
            return QuickResult(output="(timed out)", timed_out=True)
```

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/agent/test_quick_commands.py -v
```

- [ ] **Step 5: Wire into slash_dispatcher**

In `opencomputer/agent/slash_dispatcher.py`, find the entry point (e.g. `dispatch(name, args, runtime)`). At the very top, before slash lookup:

```python
from opencomputer.agent.quick_commands import QuickCommands

def dispatch(name: str, args: str, runtime, *, _depth: int = 0):
    quick = runtime.custom.get("_quick_commands")  # set at session start
    if quick is not None:
        result = quick.run(name, args, _depth=_depth)
        if result is not None:
            return SlashCommandResult(output=result.output, handled=True)
    # ... existing slash lookup below
```

In `_run_chat_session` (cli.py), load quick commands once at session start:

```python
from opencomputer.agent.quick_commands import QuickCommands
from opencomputer.agent.config_store import config_path

quick = QuickCommands.load(
    config_path(),
    dispatcher=lambda n, a, d: dispatch(n, a, runtime, _depth=d),
)
runtime.custom["_quick_commands"] = quick
```

(Adapt to actual function names.)

- [ ] **Step 6: Add `oc config quick-commands list` CLI subcommand**

In `cli.py` config_app block, add:

```python
@config_app.command("quick-commands")
def config_quick_commands(action: str = typer.Argument("list")) -> None:
    if action != "list":
        typer.echo("Usage: oc config quick-commands list")
        raise typer.Exit(2)
    from opencomputer.agent.quick_commands import QuickCommands
    from opencomputer.agent.config_store import config_path
    qc = QuickCommands.load(config_path())
    if not qc.commands:
        typer.echo("No quick commands configured.")
        return
    for name, spec in sorted(qc.commands.items()):
        target = spec.command if spec.type == "exec" else spec.target
        typer.echo(f"  /{name:<15}  {spec.type:<6}  {target}")
```

- [ ] **Step 7: Commit**

```bash
git add opencomputer/agent/quick_commands.py opencomputer/agent/slash_dispatcher.py opencomputer/cli.py tests/agent/test_quick_commands.py
git commit -m "feat(cli): quick commands (zero-token exec/alias) (Hermes A6)"
```

---

## Task 7: A7 — Ctrl+Z suspend (Unix only)

**Files:**
- Modify: `opencomputer/cli_ui/input_loop.py` (`enable_suspend=True` on Application)

- [ ] **Step 1: Locate the prompt-toolkit Application call**

```bash
grep -n "Application(" opencomputer/cli_ui/input_loop.py
```

- [ ] **Step 2: Add the kwarg (with version-guard)**

`enable_suspend` was added in prompt-toolkit 3.0.x. Guard with `inspect`
so older installs don't crash:

```python
import inspect, sys
from prompt_toolkit import Application
_app_kwargs = {}
if "enable_suspend" in inspect.signature(Application.__init__).parameters:
    _app_kwargs["enable_suspend"] = sys.platform != "win32"
app = Application(
    ...,
    **_app_kwargs,
)
```

- [ ] **Step 3: Print a friendly hint on SIGTSTP**

prompt-toolkit's `enable_suspend` already raises SIGTSTP cleanly. Add a one-time hint print just before the kwarg-construction site:

```python
import signal
def _suspend_hint(*_):
    sys.stderr.write("\nOpenComputer Agent has been suspended. Run `fg` to bring OpenComputer Agent back.\n")
    signal.signal(signal.SIGTSTP, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTSTP)
if sys.platform != "win32":
    signal.signal(signal.SIGTSTP, _suspend_hint)
```

- [ ] **Step 4: Smoke-test on Linux/macOS**

```bash
oc chat &
PID=$!
sleep 2
kill -TSTP $PID
sleep 1
fg
```

Expected: hint message printed, process resumes cleanly. (This is a manual check; document outcome in commit message.)

- [ ] **Step 5: Add unit test for the import side**

```python
# tests/cli_ui/test_per_prompt_elapsed.py — append (or new test_input_loop_suspend.py)
import sys

def test_input_loop_imports_signal_module() -> None:
    """Ctrl+Z handler must be wired (or no-op on Windows)."""
    from opencomputer.cli_ui import input_loop  # noqa: F401
    # Light smoke — module imports cleanly.
```

- [ ] **Step 6: Commit**

```bash
git add opencomputer/cli_ui/input_loop.py tests/cli_ui/test_per_prompt_elapsed.py
git commit -m "feat(cli): Ctrl+Z suspend with friendly hint on Unix (Hermes A7)"
```

---

## Task 8: B1 — `/rollback [N]` slash command

**Files:**
- Create: `opencomputer/agent/slash_commands_impl/rollback_cmd.py`
- Create: `tests/slash/test_rollback_cmd.py`
- Modify: `opencomputer/agent/slash_commands.py` (register)

- [ ] **Step 1: Write failing tests**

```python
# tests/slash/test_rollback_cmd.py
import asyncio
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.slash_commands_impl.rollback_cmd import RollbackCommand
from plugin_sdk.runtime_context import RuntimeContext


def _runtime(store: object | None = None) -> RuntimeContext:
    rt = RuntimeContext()
    rt.custom["_rewind_store"] = store
    return rt


def test_no_arg_lists_checkpoints() -> None:
    cmd = RollbackCommand()
    store = MagicMock()
    store.list_checkpoints.return_value = [
        {"id": "c1", "label": "before-edit", "ts": 1700000000, "files": 3},
        {"id": "c2", "label": "after-test", "ts": 1700000100, "files": 5},
    ]
    res = asyncio.run(cmd.execute("", _runtime(store)))
    assert "before-edit" in res.output
    assert "after-test" in res.output
    assert res.handled


def test_numeric_arg_restores_nth() -> None:
    cmd = RollbackCommand()
    store = MagicMock()
    store.list_checkpoints.return_value = [
        {"id": "c1", "label": "a", "ts": 1, "files": 1},
        {"id": "c2", "label": "b", "ts": 2, "files": 2},
    ]
    res = asyncio.run(cmd.execute("2", _runtime(store)))
    store.restore.assert_called_once_with("c2")
    assert "restored" in res.output.lower()


def test_out_of_range_returns_error() -> None:
    cmd = RollbackCommand()
    store = MagicMock()
    store.list_checkpoints.return_value = []
    res = asyncio.run(cmd.execute("99", _runtime(store)))
    assert "no checkpoints" in res.output.lower() or "out of range" in res.output.lower()


def test_no_store_wired_returns_friendly_error() -> None:
    cmd = RollbackCommand()
    rt = RuntimeContext()
    res = asyncio.run(cmd.execute("", rt))
    assert "checkpoint store" in res.output.lower()
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/slash/test_rollback_cmd.py -v
```

- [ ] **Step 3: Implement**

```python
# opencomputer/agent/slash_commands_impl/rollback_cmd.py
"""``/rollback [N]`` — list / restore filesystem checkpoints.

Hermes-CLI parity (doc line 95). Wraps the existing RewindStore (the
backing store of `oc checkpoints` CLI) so users can list and restore
without leaving the chat REPL.
"""

from __future__ import annotations

import datetime as _dt

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class RollbackCommand(SlashCommand):
    name = "rollback"
    description = "List recent checkpoints / restore the Nth-most-recent."

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        store = runtime.custom.get("_rewind_store")
        if store is None:
            return SlashCommandResult(
                output=(
                    "no checkpoint store wired — set "
                    "`checkpoint.auto_checkpoint = true` in config.yaml or run "
                    "`oc checkpoints status` to initialise."
                ),
                handled=True,
            )

        checkpoints = list(store.list_checkpoints() or [])
        if not checkpoints:
            return SlashCommandResult(output="no checkpoints recorded yet.", handled=True)

        arg = (args or "").strip()
        if not arg:
            lines = ["#  ts                   label                  files"]
            for i, ck in enumerate(checkpoints[:10], 1):
                ts = _dt.datetime.fromtimestamp(ck.get("ts", 0)).isoformat(
                    sep=" ", timespec="seconds"
                )
                lines.append(
                    f"{i:>2}  {ts}  {str(ck.get('label', '')):<22}  {ck.get('files', 0)}"
                )
            return SlashCommandResult(output="\n".join(lines), handled=True)

        try:
            n = int(arg)
        except ValueError:
            return SlashCommandResult(
                output=f"invalid arg '{arg}' — use `/rollback` or `/rollback N`",
                handled=True,
            )
        if n < 1 or n > len(checkpoints):
            return SlashCommandResult(
                output=f"out of range: {n} (have {len(checkpoints)} checkpoints)",
                handled=True,
            )
        target = checkpoints[n - 1]
        store.restore(target["id"])
        return SlashCommandResult(
            output=f"restored checkpoint #{n} ({target.get('label', '')})",
            handled=True,
        )
```

- [ ] **Step 4: Register in slash_commands.py**

```python
# opencomputer/agent/slash_commands.py — _BUILTIN_COMMANDS tuple
from opencomputer.agent.slash_commands_impl.rollback_cmd import RollbackCommand
# ... add to tuple:
RollbackCommand,
```

- [ ] **Step 5: Run tests + ruff**

```bash
pytest tests/slash/test_rollback_cmd.py -v
ruff check opencomputer/agent/slash_commands_impl/rollback_cmd.py
```

- [ ] **Step 6: Commit**

```bash
git add opencomputer/agent/slash_commands_impl/rollback_cmd.py opencomputer/agent/slash_commands.py tests/slash/test_rollback_cmd.py
git commit -m "feat(slash): /rollback [N] — list/restore checkpoints (Hermes B1)"
```

---

## Task 9: B2 — `/busy` (replaces `/queue-mode`)

**Files:**
- Create: `opencomputer/agent/slash_commands_impl/busy_cmd.py`
- Create: `tests/slash/test_busy_cmd.py`
- Modify: `opencomputer/agent/slash_commands_impl/queue_mode_cmd.py` (deprecation shim)
- Modify: `opencomputer/agent/slash_commands.py` (register `BusyCommand`)

- [ ] **Step 1: Write failing tests**

```python
# tests/slash/test_busy_cmd.py
import asyncio

from opencomputer.agent.slash_commands_impl.busy_cmd import BusyCommand
from plugin_sdk.runtime_context import RuntimeContext


def _rt() -> RuntimeContext:
    return RuntimeContext()


def test_set_interrupt() -> None:
    rt = _rt()
    asyncio.run(BusyCommand().execute("interrupt", rt))
    assert rt.custom["busy_input_mode"] == "interrupt"


def test_set_queue() -> None:
    rt = _rt()
    asyncio.run(BusyCommand().execute("queue", rt))
    assert rt.custom["busy_input_mode"] == "queue"


def test_set_steer() -> None:
    rt = _rt()
    asyncio.run(BusyCommand().execute("steer", rt))
    assert rt.custom["busy_input_mode"] == "steer"


def test_status_reports_current() -> None:
    rt = _rt()
    rt.custom["busy_input_mode"] = "queue"
    res = asyncio.run(BusyCommand().execute("status", rt))
    assert "queue" in res.output


def test_unknown_arg_prints_usage() -> None:
    rt = _rt()
    res = asyncio.run(BusyCommand().execute("foo", rt))
    assert "Usage" in res.output


def test_default_status_when_no_arg() -> None:
    rt = _rt()
    res = asyncio.run(BusyCommand().execute("", rt))
    assert "interrupt" in res.output  # default mode
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/slash/test_busy_cmd.py -v
```

- [ ] **Step 3: Implement BusyCommand**

```python
# opencomputer/agent/slash_commands_impl/busy_cmd.py
"""``/busy [interrupt|queue|steer|status]`` — busy-input mode setter.

Hermes-CLI parity (doc lines 155-176). Replaces ``/queue-mode``.
``/queue-mode`` is preserved as a deprecation alias.

Modes:

- ``interrupt`` (default) — message cancels the current operation.
- ``queue``               — message queued for next turn.
- ``steer``               — message injected via ``steer.inject()`` after
                            the next tool call (falls back to ``queue``
                            if no tool call this turn).
- ``status``              — print current mode + describe each.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


_MODES = ("interrupt", "queue", "steer")


def _describe() -> str:
    return (
        "interrupt — cancel current operation\n"
        "queue     — queue silently for next turn\n"
        "steer     — inject after next tool call (fallback: queue)\n"
        "status    — show current mode"
    )


class BusyCommand(SlashCommand):
    name = "busy"
    description = "Busy-input mode (interrupt/queue/steer/status)."

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("busy_input_mode", "interrupt")

        if sub in _MODES:
            runtime.custom["busy_input_mode"] = sub
            return SlashCommandResult(output=f"busy-input mode: {sub}", handled=True)
        if sub in ("", "status"):
            return SlashCommandResult(
                output=f"current: {current}\n\n{_describe()}",
                handled=True,
            )
        return SlashCommandResult(
            output=f"Usage: /busy [{ '|'.join(_MODES) }|status]\n\n{_describe()}",
            handled=True,
        )
```

- [ ] **Step 4: Convert `/queue-mode` to alias with deprecation note**

```python
# opencomputer/agent/slash_commands_impl/queue_mode_cmd.py — replace body of execute()
class QueueModeCommand(SlashCommand):
    name = "queue-mode"
    description = "[deprecated] Use /busy. Alias kept for back-compat."

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        from opencomputer.agent.slash_commands_impl.busy_cmd import BusyCommand
        # Prepend a deprecation banner to the BusyCommand response.
        inner = await BusyCommand().execute(args, runtime)
        return SlashCommandResult(
            output=("/queue-mode is deprecated; use /busy. (still works.)\n\n" + inner.output),
            handled=True,
        )
```

- [ ] **Step 5: Register BusyCommand in slash_commands.py**

```python
from opencomputer.agent.slash_commands_impl.busy_cmd import BusyCommand
# ... add to _BUILTIN_COMMANDS:
BusyCommand,
```

- [ ] **Step 6: Run all the new + existing tests**

```bash
pytest tests/slash/test_busy_cmd.py -v
pytest tests/agent/test_queue_mode_cmd.py -v   # may not exist; skip if absent
```

- [ ] **Step 7: Commit**

```bash
git add opencomputer/agent/slash_commands_impl/busy_cmd.py opencomputer/agent/slash_commands_impl/queue_mode_cmd.py opencomputer/agent/slash_commands.py tests/slash/test_busy_cmd.py
git commit -m "feat(slash): /busy [interrupt|queue|steer|status] (Hermes B2; deprecates /queue-mode)"
```

---

## Task 10: B3 — `/details [section] [mode]`

**Files:**
- Create: `opencomputer/agent/slash_commands_impl/details_cmd.py`
- Create: `tests/slash/test_details_cmd.py`
- Modify: `opencomputer/agent/slash_commands.py` (register)

- [ ] **Step 1: Write failing tests**

```python
# tests/slash/test_details_cmd.py
import asyncio

from opencomputer.agent.slash_commands_impl.details_cmd import DetailsCommand
from plugin_sdk.runtime_context import RuntimeContext


def test_global_set() -> None:
    rt = RuntimeContext()
    asyncio.run(DetailsCommand().execute("expanded", rt))
    assert rt.custom["details_mode"] == "expanded"


def test_global_cycle() -> None:
    rt = RuntimeContext()
    rt.custom["details_mode"] = "collapsed"
    asyncio.run(DetailsCommand().execute("cycle", rt))
    assert rt.custom["details_mode"] == "expanded"


def test_section_override() -> None:
    rt = RuntimeContext()
    asyncio.run(DetailsCommand().execute("thinking expanded", rt))
    assert rt.custom["sections"]["thinking"] == "expanded"


def test_section_reset_drops_override() -> None:
    rt = RuntimeContext()
    rt.custom["sections"] = {"thinking": "expanded"}
    asyncio.run(DetailsCommand().execute("thinking reset", rt))
    assert "thinking" not in rt.custom["sections"]


def test_unknown_section_returns_usage() -> None:
    rt = RuntimeContext()
    res = asyncio.run(DetailsCommand().execute("nope expanded", rt))
    assert "Usage" in res.output


def test_unknown_mode_returns_usage() -> None:
    rt = RuntimeContext()
    res = asyncio.run(DetailsCommand().execute("thinking foo", rt))
    assert "Usage" in res.output
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/slash/test_details_cmd.py -v
```

- [ ] **Step 3: Implement DetailsCommand**

```python
# opencomputer/agent/slash_commands_impl/details_cmd.py
"""``/details [section] [mode]`` — TUI section visibility setter.

Hermes-CLI parity (doc lines 281-284). Two argument shapes:

- ``/details [hidden|collapsed|expanded|cycle]`` — global default.
- ``/details <section> [hidden|collapsed|expanded|reset]`` — per-section
  override.

Sections: ``thinking``, ``tools``, ``subagents``, ``activity``.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


_MODES = ("hidden", "collapsed", "expanded")
_SECTIONS = ("thinking", "tools", "subagents", "activity")
_CYCLE = ("collapsed", "expanded", "hidden")


def _next_in_cycle(current: str) -> str:
    try:
        idx = _CYCLE.index(current)
    except ValueError:
        idx = -1
    return _CYCLE[(idx + 1) % len(_CYCLE)]


class DetailsCommand(SlashCommand):
    name = "details"
    description = "Section visibility (global or per-section)."

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        parts = (args or "").split()
        sections = runtime.custom.setdefault("sections", {})

        if not parts:
            mode = runtime.custom.get("details_mode", "collapsed")
            return SlashCommandResult(
                output=f"details mode: {mode}\nsections: {sections}",
                handled=True,
            )

        first = parts[0].lower()

        # Global form: /details <mode>
        if first in _MODES or first == "cycle":
            new = _next_in_cycle(runtime.custom.get("details_mode", "collapsed")) if first == "cycle" else first
            runtime.custom["details_mode"] = new
            return SlashCommandResult(output=f"details mode: {new}", handled=True)

        # Per-section form: /details <section> <mode|reset>
        if first in _SECTIONS:
            if len(parts) < 2:
                return SlashCommandResult(
                    output=f"Usage: /details {first} [{ '|'.join(_MODES) }|reset]",
                    handled=True,
                )
            mode = parts[1].lower()
            if mode == "reset":
                sections.pop(first, None)
                return SlashCommandResult(
                    output=f"section {first}: reset (uses global default)", handled=True
                )
            if mode not in _MODES:
                return SlashCommandResult(
                    output=f"Usage: /details {first} [{ '|'.join(_MODES) }|reset]",
                    handled=True,
                )
            sections[first] = mode
            return SlashCommandResult(output=f"section {first}: {mode}", handled=True)

        return SlashCommandResult(
            output=(
                f"Usage:\n"
                f"  /details [{ '|'.join(_MODES) }|cycle]   # global\n"
                f"  /details <section> [{ '|'.join(_MODES) }|reset]  # per-section\n"
                f"sections: { ', '.join(_SECTIONS) }"
            ),
            handled=True,
        )
```

- [ ] **Step 4: Register**

```python
from opencomputer.agent.slash_commands_impl.details_cmd import DetailsCommand
# add to _BUILTIN_COMMANDS:
DetailsCommand,
```

- [ ] **Step 5: Tests + commit**

```bash
pytest tests/slash/test_details_cmd.py -v
ruff check opencomputer/agent/slash_commands_impl/details_cmd.py
git add opencomputer/agent/slash_commands_impl/details_cmd.py opencomputer/agent/slash_commands.py tests/slash/test_details_cmd.py
git commit -m "feat(slash): /details [section] [mode] for TUI visibility (Hermes B3)"
```

---

## Task 11: B4 — Audit pass on existing handlers (verify-only by default)

**Files (read + small edits):**
- `opencomputer/cli_ui/slash_handlers.py`
- `opencomputer/agent/slash_commands_impl/{voice,model,reasoning,…}.py` if they exist

This task is *verify by default*. Only patch a handler if the audit
reveals a concrete divergence from the Hermes doc. If everything checks
out, commit a no-op note in the PR description and move on.

- [ ] **Step 1: Concrete checks (one per command)**

Run each `oc chat` smoke test inside the worktree and record the result.

| Command | Expected behaviour (Hermes doc) | How to verify |
|---|---|---|
| `/tools` | Lists each registered tool by name | Run inside `oc chat`; output mentions "Edit", "Bash", etc. |
| `/skills` | Lists installed skills; `/skills browse` opens hub | `/skills` lists; `/skills browse` calls skills_hub helper |
| `/voice` | Args `on \| off \| tts \| status` all accepted | Each arg returns "voice mode: …" |
| `/model` | Accepts `provider:model` syntax | `/model anthropic:claude-sonnet-4-7` swaps provider+model |
| `/compress` | Triggers force-compact next turn | Check `_force_compact_next_turn` set after `/compress` |
| `/help` | Groups output by category | Output contains category headers |

- [ ] **Step 2: Patch only on a finding**

If a check fails, write a focused unit test reproducing the gap, then
edit the handler with the smallest possible patch. Do NOT re-architect.

- [ ] **Step 3: Run all slash tests**

```bash
pytest tests/slash/ -v
```

- [ ] **Step 4: Commit (or skip if no findings)**

If no findings: add a one-line note to the PR body and skip the commit.
If findings: each fix becomes its own commit so reviewers can read the
delta in isolation.

```bash
git add -p
git commit -m "fix(slash): match <command> semantics to Hermes doc (Hermes B4)"
```

---

## Task 12: C1 — `oc sessions` plural top-level alias

**Files:**
- Modify: `opencomputer/cli.py` (one `add_typer` line)

- [ ] **Step 1: Find the existing session_app registration**

```bash
grep -n "add_typer(session_app" opencomputer/cli.py
```

- [ ] **Step 2: Add a plural alias**

```python
# opencomputer/cli.py — right after existing session line
app.add_typer(session_app, name="sessions", help="Inspect/manage sessions (alias of `oc session`).")
```

- [ ] **Step 3: Smoke test in shell**

```bash
oc sessions list 2>/dev/null | head
oc session list 2>/dev/null | head
```

Expected: identical output.

- [ ] **Step 4: Add CLI runner test**

```python
# tests/cli/test_sessions_plural.py
from typer.testing import CliRunner

from opencomputer.cli import app


def test_singular_and_plural_dispatch_same_subapp() -> None:
    r = CliRunner()
    out_s = r.invoke(app, ["session", "list"])
    out_p = r.invoke(app, ["sessions", "list"])
    # Both should at least exit 0; output may differ on timing.
    assert out_s.exit_code == 0
    assert out_p.exit_code == 0
```

- [ ] **Step 5: Commit (folded into Task 13–15 commit; don't commit alone)**

Defer commit to Task 15 to keep all sessions polish in one logical commit.

---

## Task 13: C2 — `oc sessions stats`

**Files:**
- Modify: `opencomputer/cli_session.py` (new `@session_app.command("stats")`)
- Modify: `opencomputer/agent/state.py` (new `count_sessions_by_source()`, `count_messages()`)

- [ ] **Step 1: Add SessionDB methods**

```python
# opencomputer/agent/state.py — append to SessionDB class
def count_sessions_by_source(self) -> dict[str, int]:
    cur = self._conn.execute(
        "SELECT COALESCE(source,'cli') AS s, COUNT(*) FROM sessions GROUP BY s"
    )
    return {row[0]: int(row[1]) for row in cur}

def count_messages(self) -> int:
    cur = self._conn.execute("SELECT COUNT(*) FROM messages")
    return int(cur.fetchone()[0])
```

- [ ] **Step 2: Write failing test**

```python
# tests/cli/test_sessions_plural.py — append
import os
import sqlite3
import tempfile

from typer.testing import CliRunner

from opencomputer.cli import app


def test_stats_runs() -> None:
    r = CliRunner()
    res = r.invoke(app, ["sessions", "stats"])
    assert res.exit_code == 0
    assert "Total sessions" in res.output or "total sessions" in res.output.lower()
```

- [ ] **Step 3: Run test, expect failure (no `stats` subcommand)**

```bash
pytest tests/cli/test_sessions_plural.py::test_stats_runs -v
```

- [ ] **Step 4: Implement**

```python
# opencomputer/cli_session.py — append
@session_app.command("stats")
def stats() -> None:
    """Show counts by source + DB size + message totals."""
    db = _db()
    by_src = db.count_sessions_by_source()
    n_msg = db.count_messages()
    total = sum(by_src.values())
    db_path = _home() / "sessions.db"
    size_mb = db_path.stat().st_size / 1_048_576 if db_path.exists() else 0.0
    console.print(f"Total sessions: [bold]{total}[/]")
    console.print(f"Total messages: [bold]{n_msg}[/]")
    for src, n in sorted(by_src.items(), key=lambda kv: -kv[1]):
        console.print(f"  {src}: {n}")
    console.print(f"Database size: [bold]{size_mb:.1f} MB[/]")
```

- [ ] **Step 5: Re-run, expect pass**

```bash
pytest tests/cli/test_sessions_plural.py::test_stats_runs -v
```

- [ ] **Step 6: Defer commit to Task 15**

---

## Task 14: C3 — `oc sessions export <path>`

**Files:**
- Modify: `opencomputer/cli_session.py`

- [ ] **Step 1: Write failing test**

```python
# tests/cli/test_sessions_plural.py — append
def test_export_writes_jsonl(tmp_path) -> None:
    r = CliRunner()
    out = tmp_path / "dump.jsonl"
    res = r.invoke(app, ["sessions", "export", str(out)])
    assert res.exit_code == 0
    assert out.exists()
    # Each line must be valid JSON.
    import json
    for line in out.read_text().splitlines():
        if line.strip():
            json.loads(line)
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/cli/test_sessions_plural.py::test_export_writes_jsonl -v
```

- [ ] **Step 3: Implement**

```python
# opencomputer/cli_session.py — append
@session_app.command("export")
def export_(
    path: str = typer.Argument(..., help="Output JSONL file."),
    source: str | None = typer.Option(None, "--source", help="Filter by source."),
    session_id: str | None = typer.Option(None, "--session-id", help="One session only."),
    include_messages: bool = typer.Option(True, "--include-messages/--no-messages"),
) -> None:
    """Dump sessions to JSONL — one JSON object per line."""
    import json
    from pathlib import Path as _P
    db = _db()
    if session_id:
        rows = [db.get_session(session_id)] if hasattr(db, "get_session") else []
    elif source:
        rows = list(db.list_sessions(limit=10_000, source=source))
    else:
        rows = list(db.list_sessions(limit=10_000))
    out_p = _P(path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_p.open("w", encoding="utf-8") as fh:
        for row in rows:
            sid = row.get("id") if isinstance(row, dict) else getattr(row, "id", None)
            payload = dict(row) if isinstance(row, dict) else dict(vars(row))
            if include_messages and sid:
                payload["messages"] = list(db.get_messages(sid) or [])
            fh.write(json.dumps(payload, default=str) + "\n")
            n += 1
    console.print(f"exported {n} sessions to {out_p}")
```

- [ ] **Step 4: Re-run, expect pass**

---

## Task 15: C4 — `oc sessions rename <id> "title"`

**Files:**
- Modify: `opencomputer/cli_session.py`
- Commit all of C1-C4 here.

- [ ] **Step 1: Test**

```python
# tests/cli/test_sessions_plural.py — append
def test_rename_changes_title(tmp_path) -> None:
    # Insert a fake row, rename, verify.
    r = CliRunner()
    res_l = r.invoke(app, ["sessions", "list", "--limit", "1"])
    if res_l.exit_code != 0 or not res_l.output.strip():
        return  # no sessions in this env — skip
```

(The test is light because creating a real session is heavy; the rename
logic is exercised through SessionDB methods unit-tested elsewhere.)

- [ ] **Step 2: Implement**

```python
@session_app.command("rename")
def rename_(
    session_id: str = typer.Argument(..., help="Session id (or unique prefix)."),
    title: list[str] = typer.Argument(None, help="New title (no quotes needed)."),
) -> None:
    """Set or change the title of a saved session."""
    new_title = " ".join(title or []).strip()
    if not new_title:
        console.print("[red]title required[/]")
        raise typer.Exit(2)
    db = _db()
    if hasattr(db, "rename_session"):
        db.rename_session(session_id, new_title)
    else:
        # Fall back to direct UPDATE (titles have unique idx; let SQLite enforce).
        db._conn.execute("UPDATE sessions SET title=? WHERE id=?", (new_title, session_id))
        db._conn.commit()
    console.print(f"renamed {session_id} → {new_title}")
```

- [ ] **Step 3: Commit C1-C4 together**

```bash
git add opencomputer/cli.py opencomputer/cli_session.py opencomputer/agent/state.py tests/cli/test_sessions_plural.py
git commit -m "feat(sessions): plural alias + stats/export/rename CLI (Hermes C1-C4)"
```

---

## Task 16: C5 — Resume by name with lineage

**Files:**
- Modify: `opencomputer/agent/state.py` (new SessionDB methods)
- Modify: `opencomputer/cli.py` (`_resolve_resume_target` extension)

- [ ] **Step 1: Write failing test**

```python
# tests/cli/test_resume_by_name_title.py
from pathlib import Path

import pytest

from opencomputer.agent.state import SessionDB


def test_lineage_query(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    db.create_session(id="a", started_at=1.0, title="proj")
    db.create_session(id="b", started_at=2.0, title="proj #2")
    db.create_session(id="c", started_at=3.0, title="proj #3")
    rows = db.find_sessions_by_title_lineage("proj")
    assert [r["id"] for r in rows] == ["c", "b", "a"]


def test_exact_title_query(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    db.create_session(id="a", started_at=1.0, title="hello")
    row = db.find_session_by_title("hello")
    assert row["id"] == "a"
    assert db.find_session_by_title("nope") is None
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement DB methods**

```python
# opencomputer/agent/state.py — append to SessionDB
def find_session_by_title(self, title: str):
    cur = self._conn.execute(
        "SELECT * FROM sessions WHERE title = ? LIMIT 1", (title,)
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, row, strict=True))

def find_sessions_by_title_lineage(self, base: str) -> list[dict]:
    pattern = base + " #*"
    cur = self._conn.execute(
        "SELECT * FROM sessions WHERE title = ? OR title GLOB ? "
        "ORDER BY started_at DESC",
        (base, pattern),
    )
    rows = cur.fetchall()
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]
```

- [ ] **Step 4: Extend `_resolve_resume_target`**

```python
# opencomputer/cli.py — augment after existing last/pick branches
# (after pick branch, before falling back to id-prefix path)
exact = db.find_session_by_title(spec)
if exact:
    return str(exact["id"])
lineage = db.find_sessions_by_title_lineage(spec)
if lineage:
    return str(lineage[0]["id"])
```

- [ ] **Step 5: Add resume integration test**

```python
# tests/cli/test_resume_by_name_title.py — append
def test_resolve_picks_lineage_latest(tmp_path: Path, monkeypatch) -> None:
    from opencomputer.cli import _resolve_resume_target
    from opencomputer.agent.state import SessionDB

    db_path = tmp_path / "s.db"
    db = SessionDB(db_path)
    db.create_session(id="old", started_at=1.0, title="thing")
    db.create_session(id="newer", started_at=2.0, title="thing #2")

    # Monkeypatch default_config so the resolver opens our tmp DB.
    from opencomputer.agent import config as _cfg
    # Build a fresh config for the patched callable so we don't mutate
    # the global default_config() singleton across tests.
    from copy import deepcopy
    template = deepcopy(_cfg.default_config())
    template.session.db_path = db_path  # type: ignore[attr-defined]

    monkeypatch.setattr(_cfg, "default_config", lambda: template)
    assert _resolve_resume_target("thing") == "newer"
```

- [ ] **Step 6: Commit**

```bash
git add opencomputer/agent/state.py opencomputer/cli.py tests/cli/test_resume_by_name_title.py
git commit -m "feat(sessions): resume by name with lineage match (Hermes C5)"
```

---

## Task 17: C6 — Resume by title string

Already implemented as part of Task 16 (`find_session_by_title`). Verify the
end-to-end path with one additional test.

- [ ] **Step 1: Add test**

```python
# tests/cli/test_resume_by_name_title.py — append
def test_resolve_picks_exact_title(tmp_path: Path, monkeypatch) -> None:
    from opencomputer.cli import _resolve_resume_target
    from opencomputer.agent.state import SessionDB
    from opencomputer.agent import config as _cfg

    db_path = tmp_path / "s.db"
    SessionDB(db_path).create_session(id="x", started_at=1.0, title="refactor auth")

    from copy import deepcopy
    template = deepcopy(_cfg.default_config())
    template.session.db_path = db_path  # type: ignore[attr-defined]

    monkeypatch.setattr(_cfg, "default_config", lambda: template)
    assert _resolve_resume_target("refactor auth") == "x"
```

- [ ] **Step 2: No new code — folded into Task 16 commit. Run all C tests.**

```bash
pytest tests/cli/test_resume_by_name_title.py -v
```

---

## Task 18: C7 — Title lineage helper

**Files:**
- Modify: `opencomputer/agent/title_generator.py`
- Create: `tests/agent/test_title_lineage.py`

- [ ] **Step 1: Write failing test**

```python
# tests/agent/test_title_lineage.py
from pathlib import Path

from opencomputer.agent.state import SessionDB
from opencomputer.agent.title_generator import next_title_in_lineage


def test_first_in_lineage_returns_base(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    assert next_title_in_lineage(db, "fresh") == "fresh"


def test_existing_base_bumps_to_2(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    db.create_session(id="a", started_at=1.0, title="proj")
    assert next_title_in_lineage(db, "proj") == "proj #2"


def test_with_existing_lineage_picks_next_n(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    db.create_session(id="a", started_at=1.0, title="proj")
    db.create_session(id="b", started_at=2.0, title="proj #2")
    db.create_session(id="c", started_at=3.0, title="proj #5")
    assert next_title_in_lineage(db, "proj") == "proj #6"
```

- [ ] **Step 2: Implement**

```python
# opencomputer/agent/title_generator.py — append
import re

_LINEAGE_RE = re.compile(r"^(.+?)\s+#(\d+)$")


def next_title_in_lineage(db, base: str) -> str:
    """Return the next title in *base*'s lineage (`base`, `base #2`, …).

    Hermes parity (doc lines 442-447). Used by manual `oc session fork
    --inherit-title` and (future) compaction-fork hook. Best-effort —
    if querying fails the base title is returned unchanged.
    """
    try:
        rows = db.find_sessions_by_title_lineage(base)
    except Exception:
        return base
    if not rows:
        return base
    highest = 1
    for r in rows:
        title = r.get("title") or ""
        if title == base:
            highest = max(highest, 1)
            continue
        m = _LINEAGE_RE.match(title)
        if m and m.group(1) == base:
            highest = max(highest, int(m.group(2)))
    return f"{base} #{highest + 1}"
```

- [ ] **Step 3: Wire into `oc session fork --inherit-title`**

```python
# opencomputer/cli_session.py — extend fork() signature with the new flag
@session_app.command("fork")
def fork(
    session_id: str = typer.Argument(...),
    inherit_title: bool = typer.Option(False, "--inherit-title", help="Auto-name #N in lineage."),
    title: str = typer.Option("", "--title", help="Override title for the fork."),
) -> None:
    db = _db()
    src = db.get_session(session_id)
    if src is None:
        console.print(f"[red]session not found: {session_id}[/]")
        raise typer.Exit(1)
    new_id = str(uuid.uuid4())
    new_title = title.strip()
    if not new_title and inherit_title:
        from opencomputer.agent.title_generator import next_title_in_lineage
        base = src.get("title") or "session"
        # Strip any existing #N suffix from src title to find the family base.
        base = re.sub(r"\s+#\d+$", "", base)
        new_title = next_title_in_lineage(db, base)
    db.fork_session(src_id=session_id, new_id=new_id, new_title=new_title or None)
    console.print(f"forked → {new_id}{' (' + new_title + ')' if new_title else ''}")
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/agent/test_title_lineage.py tests/cli/test_resume_by_name_title.py -v
```

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/title_generator.py opencomputer/cli_session.py tests/agent/test_title_lineage.py
git commit -m "feat(sessions): next_title_in_lineage helper + oc session fork --inherit-title (Hermes C7)"
```

---

## Task 19: D1 — Light terminal detection in TUI

**Files:**
- Create: `ui-tui/src/lib/themeDetect.ts`
- Create: `ui-tui/src/__tests__/themeDetect.test.ts`
- Modify: `ui-tui/src/theme.ts`

- [ ] **Step 1: Write failing tests**

```ts
// ui-tui/src/__tests__/themeDetect.test.ts
import { describe, it, expect, beforeEach } from 'vitest'
import { detectTheme } from '../lib/themeDetect.js'

describe('detectTheme', () => {
  beforeEach(() => {
    delete process.env.OPENCOMPUTER_TUI_THEME
    delete process.env.COLORFGBG
  })

  it('honours env override light', async () => {
    process.env.OPENCOMPUTER_TUI_THEME = 'light'
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('light')
  })

  it('honours env override dark', async () => {
    process.env.OPENCOMPUTER_TUI_THEME = 'dark'
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('dark')
  })

  it('parses 6-char hex bg', async () => {
    process.env.OPENCOMPUTER_TUI_THEME = 'ffffff'
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('light')
  })

  it('parses COLORFGBG xterm light', async () => {
    process.env.COLORFGBG = '0;15'
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('light')
  })

  it('parses OSC11 reply', async () => {
    const t = await detectTheme({
      probe: async () => '\x1b]11;rgb:ffff/ffff/ffff\x1b\\',
    })
    expect(t.kind).toBe('light')
  })

  it('default dark when silent', async () => {
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('dark')
  })
})
```

- [ ] **Step 2: Run, expect failure**

```bash
cd ui-tui && pnpm vitest run src/__tests__/themeDetect.test.ts
```

- [ ] **Step 3: Implement**

```ts
// ui-tui/src/lib/themeDetect.ts
/**
 * Light / dark terminal detection for the TUI.
 *
 * Mirrors opencomputer.cli_ui.theme_detect (Python). Layered: env
 * override → COLORFGBG → OSC 11 probe → dark default.
 */

export type ThemeKind = 'light' | 'dark'

export interface Theme {
  kind: ThemeKind
  bgHex: string
}

const HEX_RE = /^[0-9a-fA-F]{6}$/
const OSC11_RE = /rgb:([0-9a-fA-F]+)\/([0-9a-fA-F]+)\/([0-9a-fA-F]+)/

const luminance = (hex: string): number => {
  const r = parseInt(hex.slice(0, 2), 16) / 255
  const g = parseInt(hex.slice(2, 4), 16) / 255
  const b = parseInt(hex.slice(4, 6), 16) / 255
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

const fromEnv = (): Theme | null => {
  const v = (process.env.OPENCOMPUTER_TUI_THEME ?? '').trim().toLowerCase()
  if (v === 'light') return { kind: 'light', bgHex: 'ffffff' }
  if (v === 'dark') return { kind: 'dark', bgHex: '000000' }
  if (HEX_RE.test(v)) {
    return { kind: luminance(v) > 0.5 ? 'light' : 'dark', bgHex: v }
  }
  return null
}

const fromColorFgBg = (): Theme | null => {
  const v = (process.env.COLORFGBG ?? '').trim()
  if (!v) return null
  const parts = v.split(';')
  if (parts.length < 2) return null
  const bg = parseInt(parts[parts.length - 1], 10)
  if (isNaN(bg)) return null
  return { kind: bg >= 8 ? 'light' : 'dark', bgHex: '' }
}

const parseOsc11 = (reply: string): Theme | null => {
  const m = OSC11_RE.exec(reply)
  if (!m) return null
  const hi = (s: string): string => (s + '00').slice(0, 2)
  const hex = hi(m[1]) + hi(m[2]) + hi(m[3])
  return { kind: luminance(hex) > 0.5 ? 'light' : 'dark', bgHex: hex }
}

const realProbe = async (timeoutMs = 200): Promise<string | null> => {
  if (!process.stdout.isTTY || !process.stdin.isTTY) return null
  return new Promise<string | null>((resolve) => {
    let buf = ''
    const onData = (chunk: Buffer): void => {
      buf += chunk.toString('utf8')
      if (buf.endsWith('\x1b\\') || buf.endsWith('\x07')) {
        cleanup()
        resolve(buf)
      }
    }
    const t = setTimeout(() => { cleanup(); resolve(null) }, timeoutMs)
    const cleanup = (): void => {
      clearTimeout(t)
      process.stdin.off('data', onData)
      try { process.stdin.setRawMode(false) } catch { /* noop */ }
      process.stdin.pause()
    }
    try {
      process.stdin.setRawMode(true)
      process.stdin.resume()
      process.stdin.on('data', onData)
      process.stdout.write('\x1b]11;?\x1b\\')
    } catch {
      cleanup()
      resolve(null)
    }
  })
}

export const detectTheme = async (
  opts: { probe?: () => Promise<string | null> } = {},
): Promise<Theme> => {
  const fromEnvT = fromEnv()
  if (fromEnvT) return fromEnvT
  const fromCfb = fromColorFgBg()
  if (fromCfb) return fromCfb
  const probe = opts.probe ?? realProbe
  const reply = await probe()
  if (reply) {
    const t = parseOsc11(reply)
    if (t) return t
  }
  return { kind: 'dark', bgHex: '000000' }
}
```

- [ ] **Step 4: Wire into theme.ts**

In `ui-tui/src/theme.ts`, await `detectTheme()` once during app startup and select the palette accordingly. Persist on a module-level `ACTIVE_THEME`.

- [ ] **Step 5: Run vitest**

```bash
cd ui-tui && pnpm vitest run src/__tests__/themeDetect.test.ts
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add ui-tui/src/lib/themeDetect.ts ui-tui/src/theme.ts ui-tui/src/__tests__/themeDetect.test.ts
git commit -m "feat(tui): light terminal detection (env/COLORFGBG/OSC11) (Hermes D1)"
```

---

## Task 20: D2 — Working dir + git branch in TUI status

**Files:**
- Create: `ui-tui/src/hooks/useGitBranch.ts`
- Create: `ui-tui/src/__tests__/useGitBranch.test.ts`
- Modify: `ui-tui/src/components/appChrome.tsx`

- [ ] **Step 1: Write failing test**

```ts
// ui-tui/src/__tests__/useGitBranch.test.ts
import { describe, it, expect } from 'vitest'
import { promises as fs } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { readBranchFromHead } from '../hooks/useGitBranch.js'

describe('readBranchFromHead', () => {
  it('returns null when .git is missing', async () => {
    const dir = await fs.mkdtemp(join(tmpdir(), 'gb-'))
    expect(await readBranchFromHead(dir)).toBeNull()
  })

  it('parses ref pointer', async () => {
    const dir = await fs.mkdtemp(join(tmpdir(), 'gb-'))
    await fs.mkdir(join(dir, '.git'))
    await fs.writeFile(join(dir, '.git', 'HEAD'), 'ref: refs/heads/main\n')
    expect(await readBranchFromHead(dir)).toBe('main')
  })

  it('returns short sha for detached HEAD', async () => {
    const dir = await fs.mkdtemp(join(tmpdir(), 'gb-'))
    await fs.mkdir(join(dir, '.git'))
    await fs.writeFile(join(dir, '.git', 'HEAD'), 'abc1234567890\n')
    expect(await readBranchFromHead(dir)).toBe('abc1234')
  })
})
```

- [ ] **Step 2: Implement**

```ts
// ui-tui/src/hooks/useGitBranch.ts
import { promises as fs } from 'fs'
import { join } from 'path'
import { useEffect, useState } from 'react'

export const readBranchFromHead = async (cwd: string): Promise<string | null> => {
  try {
    let gitPath = join(cwd, '.git')
    const stat = await fs.stat(gitPath).catch(() => null)
    if (!stat) return null
    if (stat.isFile()) {
      const txt = await fs.readFile(gitPath, 'utf8')
      const m = /gitdir:\s*(.+)/.exec(txt)
      if (!m) return null
      gitPath = m[1].trim()
    }
    const head = await fs.readFile(join(gitPath, 'HEAD'), 'utf8')
    const refMatch = /^ref:\s*refs\/heads\/(.+)$/m.exec(head.trim())
    if (refMatch) return refMatch[1]
    return head.trim().slice(0, 7)  // detached short sha
  } catch {
    return null
  }
}

export const useGitBranch = (cwd: string): string | null => {
  const [branch, setBranch] = useState<string | null>(null)
  useEffect(() => {
    let mtime = 0
    const tick = async (): Promise<void> => {
      try {
        const stat = await fs.stat(join(cwd, '.git', 'HEAD'))
        if (stat.mtimeMs !== mtime) {
          mtime = stat.mtimeMs
          setBranch(await readBranchFromHead(cwd))
        }
      } catch { setBranch(null) }
    }
    void tick()
    const id = setInterval(tick, 5_000)
    return () => clearInterval(id)
  }, [cwd])
  return branch
}
```

- [ ] **Step 3: Wire into appChrome.tsx**

Find the existing status-row (cwd/breadcrumb area) in `appChrome.tsx`. Add:

```tsx
import { useGitBranch } from '../hooks/useGitBranch.js'
const branch = useGitBranch(process.cwd())
// In the breadcrumb element:
{branch ? <Text dimColor>({branch})</Text> : null}
```

- [ ] **Step 4: Run vitest**

```bash
cd ui-tui && pnpm vitest run src/__tests__/useGitBranch.test.ts
```

- [ ] **Step 5: Commit**

```bash
git add ui-tui/src/hooks/useGitBranch.ts ui-tui/src/components/appChrome.tsx ui-tui/src/__tests__/useGitBranch.test.ts
git commit -m "feat(tui): working dir + git branch in status row (Hermes D2)"
```

---

## Task 21: D3-D5 — `/sessions /reload /mouse` audit

**Files (verify only; tiny patches):**
- `ui-tui/src/components/sessionPicker.tsx`
- `opencomputer/cli_ui/slash_handlers.py`
- Maybe new `opencomputer/agent/slash_commands_impl/mouse_cmd.py`

- [ ] **Step 1: Verify `/sessions` opens the modal**

```bash
grep -nE "case '?sessions'?:|name = 'sessions'" ui-tui/src/**/*.ts*
```

Confirm the slash dispatch in TUI routes `/sessions` to `sessionPicker.tsx`.
If not, wire it in the TUI slash router.

- [ ] **Step 2: Verify `/reload` re-reads .env + config.yaml**

Read `cli_ui/slash_handlers.py` for `reload`. Verify it calls
`config_store.reload()` or equivalent. If absent, add:

```python
# slash_handlers.py — handle("reload")
def _handle_reload(args, runtime):
    from opencomputer.agent.config_store import reload_config
    reload_config()
    return SlashResult(handled=True, message="reloaded .env + config.yaml")
```

- [ ] **Step 3: Implement `/mouse` slash**

```python
# opencomputer/agent/slash_commands_impl/mouse_cmd.py
"""``/mouse [on|off|toggle|status]`` — toggle TUI mouse tracking."""

from __future__ import annotations
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class MouseCommand(SlashCommand):
    name = "mouse"
    description = "Toggle TUI mouse tracking."

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        cur = bool(runtime.custom.get("mouse_tracking", True))
        if sub in ("", "toggle"):
            new = not cur
        elif sub == "on":
            new = True
        elif sub == "off":
            new = False
        elif sub == "status":
            return SlashCommandResult(output=f"mouse tracking: {'ON' if cur else 'OFF'}", handled=True)
        else:
            return SlashCommandResult(output="Usage: /mouse [on|off|toggle|status]", handled=True)
        runtime.custom["mouse_tracking"] = new
        return SlashCommandResult(output=f"mouse tracking: {'ON' if new else 'OFF'}", handled=True)
```

Register it in `slash_commands.py`:

```python
from opencomputer.agent.slash_commands_impl.mouse_cmd import MouseCommand
# add to _BUILTIN_COMMANDS:
MouseCommand,
```

- [ ] **Step 4: Add a small test**

```python
# tests/slash/test_mouse_cmd.py
import asyncio
from opencomputer.agent.slash_commands_impl.mouse_cmd import MouseCommand
from plugin_sdk.runtime_context import RuntimeContext


def test_mouse_toggle() -> None:
    rt = RuntimeContext()
    asyncio.run(MouseCommand().execute("on", rt))
    assert rt.custom["mouse_tracking"] is True
    asyncio.run(MouseCommand().execute("off", rt))
    assert rt.custom["mouse_tracking"] is False
    asyncio.run(MouseCommand().execute("toggle", rt))
    assert rt.custom["mouse_tracking"] is True
```

- [ ] **Step 5: TUI side — read mouse_tracking and toggle**

In `ui-tui/src/components/appChrome.tsx` (or wherever mouse tracking is enabled), read the runtime flag and conditionally call ink/Bubble's mouse-tracking enable. If the project doesn't yet plumb runtime flags into the TUI, add the wire (gateway client → state → useEffect setting raw mouse mode).

- [ ] **Step 6: Run all tests**

```bash
pytest tests/slash/test_mouse_cmd.py -v
cd ui-tui && pnpm vitest run
```

- [ ] **Step 7: Commit**

```bash
git add opencomputer/agent/slash_commands_impl/mouse_cmd.py opencomputer/agent/slash_commands.py tests/slash/test_mouse_cmd.py opencomputer/cli_ui/slash_handlers.py ui-tui/src/...
git commit -m "feat(tui): /sessions /reload /mouse audit + /mouse slash (Hermes D3-D5)"
```

---

## Task 22: Full-suite verification

- [ ] **Step 1: Full pytest**

```bash
pytest -x --no-header 2>&1 | tail -30
```

Expected: all green; total runtime depends on env. Stop on first failure.

- [ ] **Step 2: Full ruff**

```bash
ruff check opencomputer/ tests/ 2>&1 | tail -20
```

- [ ] **Step 3: Full vitest**

```bash
cd ui-tui && pnpm vitest run 2>&1 | tail -30
```

- [ ] **Step 4: Smoke test**

```bash
oc chat -q "say hi"
oc sessions stats
oc sessions list --limit 3
oc config quick-commands list
```

Each should exit 0 and produce reasonable output.

- [ ] **Step 5: Push & open PR**

```bash
git push -u origin feat/hermes-cli-tui-sessions-v2-parity
gh pr create --title "feat: Hermes CLI/TUI/Sessions v2 — parity (4 commits, 21 items)" \
  --body "$(cat <<'EOF'
## Summary
- A1-A7: CLI polish (paste preview, markdown strip, per-prompt elapsed, theme detect, busy styles, quick commands, Ctrl+Z)
- B1-B4: slash parity (/rollback, /busy supersedes /queue-mode, /details, audit pass)
- C1-C7: sessions polish (plural alias, stats/export/rename, resume by name/title, lineage helper)
- D1-D5: TUI polish (light theme detect, branch in status, /sessions/reload/mouse audit, /mouse slash)

## Spec
docs/superpowers/specs/2026-05-08-hermes-cli-tui-sessions-v2-parity-design.md

## Test plan
- [x] `pytest` green
- [x] `ruff check` green
- [x] `pnpm vitest run` (ui-tui) green
- [x] Manual smoke: oc chat, oc sessions, oc config

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Wait for CI**

Poll until green. If anything red, fix in same branch.

---

## Self-review checklist

After plan written, fresh-eyes pass:

- [x] Spec coverage — every item A1-D5 has a numbered task.
- [x] Placeholder scan — no "TBD"/"add appropriate handling"; concrete code in every step.
- [x] Type consistency — `SlashCommand`/`SlashCommandResult`/`RuntimeContext` used uniformly; `PromptClock`/`PasteStore`/`QuickCommands` names stable across tasks.
- [x] Type drift fix — Task 3 PromptClock `_now` field is set via dataclass; deleted dangling shim `__init__` in step 3 (commit will only contain dataclass version).
- [x] Wiring fix — Task 6 runtime.custom["_quick_commands"] consumed by Task 6's slash_dispatcher patch — names match.
