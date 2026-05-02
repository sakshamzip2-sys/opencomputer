# Hermes Onboarding Foundation (F0+F1+F2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace OC's procedural setup wizard + bare chat preamble with a Hermes-style arrow-key wizard (2 live sections + 6 deferred stubs) and a categorized welcome banner — clean-room implementation on `prompt_toolkit` + `rich`.

**Architecture:** Three new layers under `opencomputer/`:
1. `cli_ui/menu.py` (+ `style.py`) — single-source primitive: `radiolist` / `checklist` / `single_select` with arrow-key + numbered fallback.
2. `cli_setup/{wizard.py, sections.py, section_handlers/}` — section-driven orchestrator; `WizardCancelled` exception propagates ESC; sections have a `configured_check` → 3-option radiolist pattern.
3. `cli_banner.py` (+ `cli_banner_art.py`) — assembled banner for `oc chat` startup.

`opencomputer/setup_wizard.py` shrinks to a thin re-export so existing callers stay unchanged. `opencomputer/cli.py::_run_chat_session` swaps its bare preamble for `cli_banner.build_welcome_banner(...)`.

**Tech Stack:** Python 3.13, `prompt_toolkit>=3.0` (already a dep), `rich>=13.7` (already a dep), `pytest` for TDD. No new runtime deps.

**Spec:** `docs/superpowers/specs/2026-05-02-hermes-onboarding-foundation-design.md`.

**Reference impl (modeled-on, NOT copied):** `/Users/saksham/.hermes/hermes-agent/hermes_cli/{setup.py,curses_ui.py,banner.py}`.

---

## File map

| Path | Status | Lines (est) | Responsibility |
|---|---|---|---|
| `opencomputer/cli_ui/style.py` | NEW | ~40 | prompt_toolkit `Style` rules, color constants |
| `opencomputer/cli_ui/menu.py` | NEW | ~350 | `radiolist`, `checklist`, `single_select`, numbered fallbacks |
| `opencomputer/cli_ui/__init__.py` | MODIFY | +5 | Re-export `radiolist`, `checklist`, `single_select`, `WizardCancelled` |
| `opencomputer/cli_setup/__init__.py` | NEW | ~10 | Re-exports |
| `opencomputer/cli_setup/wizard.py` | NEW | ~280 | `run_setup`, `WizardCancelled`, orchestrator loop |
| `opencomputer/cli_setup/sections.py` | NEW | ~120 | `WizardSection`, `SectionResult`, `WizardCtx`, `SECTION_REGISTRY` list |
| `opencomputer/cli_setup/section_handlers/__init__.py` | NEW | ~5 | Re-exports |
| `opencomputer/cli_setup/section_handlers/_deferred.py` | NEW | ~40 | `make_deferred_handler(target_subproject)` factory |
| `opencomputer/cli_setup/section_handlers/inference_provider.py` | NEW | ~180 | Provider menu → plugin setup hook → config write |
| `opencomputer/cli_setup/section_handlers/messaging_platforms.py` | NEW | ~220 | Skip/configure radiolist → multi-select → per-platform setup |
| `opencomputer/cli_banner_art.py` | NEW | ~80 | OPENCOMPUTER ASCII art + side-glyph constants |
| `opencomputer/cli_banner.py` | NEW | ~450 | `build_welcome_banner`, `format_banner_version_label`, `get_available_skills`, `get_available_tools` |
| `opencomputer/setup_wizard.py` | MODIFY | -180/+10 | Body shrinks to re-export |
| `opencomputer/cli.py` | MODIFY | +3/-7 | `_run_chat_session` calls new banner |
| `tests/test_cli_ui_menu.py` | NEW | ~250 | Menu primitive tests |
| `tests/test_cli_setup_wizard.py` | NEW | ~280 | Orchestrator + deferred handling |
| `tests/test_cli_setup_section_inference_provider.py` | NEW | ~150 | Inference provider section |
| `tests/test_cli_setup_section_messaging_platforms.py` | NEW | ~180 | Messaging platforms section |
| `tests/test_cli_banner.py` | NEW | ~200 | Banner assembly + helpers |

Total: ~2,820 lines (~1,760 prod + ~1,060 tests).

---

## Task 1: Style module + cli_ui re-exports

**Files:**
- Create: `opencomputer/cli_ui/style.py`
- Modify: `opencomputer/cli_ui/__init__.py`

- [ ] **Step 1.1: Create `opencomputer/cli_ui/style.py`**

```python
"""prompt_toolkit Style rules for Hermes-modeled menus.

Visual register:
  - title (yellow, bold) — the "Select provider:" heading
  - hint (dim) — the navigation hint footer
  - selected (green) — current row arrow + text
  - selected.glyph (green bold) — (●) / [✓] in the selected row
  - unselected.glyph (default) — (○) / [ ] in unselected rows
  - description (dim italic) — optional description block under title

Single source for re-skinning. All menu primitives in cli_ui/menu.py
reference these class names.
"""
from __future__ import annotations

from prompt_toolkit.styles import Style

MENU_STYLE = Style.from_dict({
    "menu.title": "fg:#ffd75f bold",
    "menu.hint": "fg:#888888",
    "menu.selected": "fg:#5fff5f",
    "menu.selected.arrow": "fg:#5fff5f bold",
    "menu.selected.glyph": "fg:#5fff5f bold",
    "menu.unselected.glyph": "",
    "menu.description": "fg:#888888 italic",
})

ARROW_GLYPH = "→"
RADIO_ON = "●"
RADIO_OFF = "○"
CHECK_ON = "✓"
CHECK_OFF = " "
```

- [ ] **Step 1.2: Modify `opencomputer/cli_ui/__init__.py` — re-export menu primitives + style**

Append to existing file (after current re-exports):

```python
# Hermes-modeled menu primitives (added 2026-05-02 — F0)
from opencomputer.cli_ui.menu import (
    Choice,
    checklist,
    flush_stdin,
    radiolist,
    single_select,
)
from opencomputer.cli_ui.style import MENU_STYLE
```

Add to `__all__`:
```python
    "Choice",
    "MENU_STYLE",
    "checklist",
    "flush_stdin",
    "radiolist",
    "single_select",
```

- [ ] **Step 1.3: Run targeted import test to confirm style imports cleanly**

Run: `.venv/bin/python -c "from opencomputer.cli_ui.style import MENU_STYLE, ARROW_GLYPH, RADIO_ON; print('ok')"`
Expected: `ok` printed; no errors. (Note: cli_ui/__init__.py will fail until Task 2 lands menu.py — that's fine; we're isolating the style module here.)

- [ ] **Step 1.4: Commit**

```bash
git add opencomputer/cli_ui/style.py
git commit -m "feat(cli_ui): style module for Hermes-modeled menu primitives

Style rules + glyph constants used by F0 menu primitives in the next
commit. Single source for re-skinning so visual tweaks are one-place.

Part of F0 of the Hermes-onboarding port (spec:
docs/superpowers/specs/2026-05-02-hermes-onboarding-foundation-design.md)."
```

---

## Task 2: Menu primitive — `radiolist` (single-select)

**Files:**
- Create: `opencomputer/cli_ui/menu.py` (incremental — radiolist + Choice dataclass + numbered fallback only this task)
- Create: `tests/test_cli_ui_menu.py`

- [ ] **Step 2.1: Write the failing test for `Choice` dataclass + numbered fallback**

Create `tests/test_cli_ui_menu.py`:

```python
"""Tests for cli_ui/menu.py — arrow-key + numbered-fallback menu primitives."""
from __future__ import annotations

import io
import sys

import pytest


def test_choice_dataclass_holds_label_value_and_optional_description():
    from opencomputer.cli_ui.menu import Choice

    c = Choice(label="Anthropic", value="anthropic", description="Claude models")
    assert c.label == "Anthropic"
    assert c.value == "anthropic"
    assert c.description == "Claude models"

    c2 = Choice(label="OpenAI", value="openai")
    assert c2.description is None


def test_radiolist_numbered_fallback_returns_index_for_valid_input(monkeypatch, capsys):
    """When stdin is non-TTY, radiolist falls back to a numbered prompt."""
    from opencomputer.cli_ui.menu import Choice, radiolist

    # Force non-TTY
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("2\n"))

    choices = [
        Choice("Anthropic", "anthropic"),
        Choice("OpenAI", "openai"),
        Choice("OpenRouter", "openrouter"),
    ]

    idx = radiolist("Select provider:", choices, default=0)

    assert idx == 1, "Numbered input '2' → index 1 (1-based menu, 0-based return)"
    out = capsys.readouterr().out
    assert "Anthropic" in out and "OpenAI" in out and "OpenRouter" in out


def test_radiolist_numbered_fallback_empty_input_returns_default(monkeypatch):
    """Empty input on the numbered fallback returns the configured default."""
    from opencomputer.cli_ui.menu import Choice, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))

    choices = [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")]
    idx = radiolist("Pick:", choices, default=2)
    assert idx == 2


def test_radiolist_numbered_fallback_invalid_then_valid(monkeypatch, capsys):
    """Invalid number re-prompts; valid second answer is accepted."""
    from opencomputer.cli_ui.menu import Choice, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    # First "9" out of range, then "1"
    monkeypatch.setattr("sys.stdin", io.StringIO("9\n1\n"))

    choices = [Choice("A", "a"), Choice("B", "b")]
    idx = radiolist("Pick:", choices)
    assert idx == 0
    err = capsys.readouterr().err
    assert "out of range" in err.lower() or "invalid" in err.lower()
```

- [ ] **Step 2.2: Run failing tests**

Run: `.venv/bin/python -m pytest tests/test_cli_ui_menu.py -v`
Expected: 4 FAILs with `ModuleNotFoundError: No module named 'opencomputer.cli_ui.menu'` (or equivalent).

- [ ] **Step 2.3: Create `opencomputer/cli_ui/menu.py` with `Choice` + `radiolist` numbered fallback only**

```python
"""Arrow-key menu primitives — single source for the wizard's UX.

Visual + UX modeled after hermes-agent's hermes_cli/curses_ui.py.
Independently re-implemented on prompt_toolkit + numbered-fallback
(no code copied) — see spec § 10 O1 license decision.

Public API:
  - radiolist(question, choices, default, description) -> int
  - checklist(title, items, pre_selected) -> list[int]
  - single_select(title, items, default) -> int
  - flush_stdin() -> None

Each primitive returns the SELECTED INDEX (not the value) — caller maps
index back to the Choice via choices[idx]. ESC raises WizardCancelled
(defined in cli_setup.wizard) to propagate cancellation cleanly through
nested handlers.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Choice:
    """One menu entry. ``description`` is optional secondary text shown
    under the menu title (single-select only) or as suffix (checklist)."""

    label: str
    value: object  # opaque to menu code; caller-defined type
    description: Optional[str] = None


def flush_stdin() -> None:
    """Drain leftover keypresses before opening a prompt_toolkit Application.

    Hermes uses this to avoid stale arrow-key bytes leaking into the menu
    after returning from a previous menu (the OS terminal buffer can hold
    them between primitive calls). Implementation: best-effort, never
    raises. On non-TTY this is a no-op.
    """
    try:
        if not sys.stdin.isatty():
            return
        # Best-effort flush — termios on POSIX, no-op elsewhere.
        import termios
        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except (termios.error, OSError):
            pass
    except ImportError:
        # Windows: termios not available; no flush mechanism needed
        # because prompt_toolkit's input pipeline drains itself.
        pass


def radiolist(
    question: str,
    choices: list[Choice],
    default: int = 0,
    description: Optional[str] = None,
) -> int:
    """Single-select menu. Returns selected index.

    On TTY: arrow-key navigation via prompt_toolkit (Task 3 lands this).
    On non-TTY: numbered prompt via stdin.
    """
    if not sys.stdin.isatty():
        return _radiolist_numbered_fallback(question, choices, default, description)

    # TTY path — prompt_toolkit Application — implemented in Task 3.
    # For now (this commit only), fall through to numbered fallback so
    # this task's tests pass even on a TTY-test-environment.
    return _radiolist_numbered_fallback(question, choices, default, description)


def _radiolist_numbered_fallback(
    question: str,
    choices: list[Choice],
    default: int = 0,
    description: Optional[str] = None,
) -> int:
    """Non-TTY single-select. Prints a numbered list and reads stdin."""
    print(question)
    if description:
        print(f"  {description}")
    for i, c in enumerate(choices):
        marker = "→" if i == default else " "
        suffix = f"  ({c.description})" if c.description else ""
        print(f"  {marker} {i + 1}. {c.label}{suffix}")
    print()
    while True:
        try:
            raw = input(f"Choice [1-{len(choices)}, default {default + 1}]: ").strip()
        except EOFError:
            return default
        if raw == "":
            return default
        try:
            n = int(raw)
        except ValueError:
            print(f"Invalid input '{raw}' — enter a number.", file=sys.stderr)
            continue
        if not (1 <= n <= len(choices)):
            print(f"out of range — enter 1-{len(choices)}.", file=sys.stderr)
            continue
        return n - 1


# Stubs for checklist / single_select — implemented in subsequent tasks.
# Defined here so imports of cli_ui.menu don't error in the meantime.
def checklist(title, items, pre_selected=None):  # type: ignore[no-untyped-def]
    raise NotImplementedError("checklist lands in Task 4")


def single_select(title, items, default=0):  # type: ignore[no-untyped-def]
    raise NotImplementedError("single_select lands in Task 5")
```

- [ ] **Step 2.4: Run tests, verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli_ui_menu.py -v`
Expected: 4 PASSED.

- [ ] **Step 2.5: Commit**

```bash
git add opencomputer/cli_ui/menu.py tests/test_cli_ui_menu.py
git commit -m "feat(cli_ui): radiolist primitive + numbered fallback

Adds Choice dataclass and radiolist with numbered-fallback path. TTY
path stubbed to fall through to fallback; full prompt_toolkit
implementation lands in Task 3.

checklist / single_select stubbed with NotImplementedError so imports
don't error before their tasks ship.

Part of F0 of the Hermes-onboarding port."
```

---

## Task 3: Menu primitive — `radiolist` TTY path (prompt_toolkit Application)

**Files:**
- Modify: `opencomputer/cli_ui/menu.py` (replace radiolist's TTY-path body)
- Modify: `tests/test_cli_ui_menu.py` (add prompt_toolkit-driven tests)

- [ ] **Step 3.1: Write failing test for arrow-key TTY path using prompt_toolkit pipe-input**

Append to `tests/test_cli_ui_menu.py`:

```python
def test_radiolist_tty_arrow_down_then_enter_selects_next(monkeypatch):
    """Pipe input simulates a TTY user pressing Down + Enter."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, radiolist

    # Force TTY path
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x1b[B\r")  # Down, Enter
        idx = radiolist(
            "Pick:",
            [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")],
            default=0,
            _input=pipe_input,
            _output=DummyOutput(),
        )

    assert idx == 1, "Down from default 0 → index 1"


def test_radiolist_tty_immediate_enter_returns_default(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\r")
        idx = radiolist(
            "Pick:",
            [Choice("A", "a"), Choice("B", "b")],
            default=1,
            _input=pipe_input,
            _output=DummyOutput(),
        )

    assert idx == 1


def test_radiolist_tty_esc_raises_wizard_cancelled(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_setup.wizard import WizardCancelled
    from opencomputer.cli_ui.menu import Choice, radiolist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x1b")  # ESC
        with pytest.raises(WizardCancelled):
            radiolist(
                "Pick:",
                [Choice("A", "a")],
                _input=pipe_input,
                _output=DummyOutput(),
            )
```

Note: This test depends on `WizardCancelled` from `cli_setup.wizard` which lands in Task 6. To keep the cycle clean, define `WizardCancelled` in `cli_ui.menu` first and re-export from `cli_setup.wizard` in Task 6 (so `from opencomputer.cli_setup.wizard import WizardCancelled` is the public-facing import).

- [ ] **Step 3.2: Run tests, verify RED**

Run: `.venv/bin/python -m pytest tests/test_cli_ui_menu.py::test_radiolist_tty_arrow_down_then_enter_selects_next tests/test_cli_ui_menu.py::test_radiolist_tty_immediate_enter_returns_default tests/test_cli_ui_menu.py::test_radiolist_tty_esc_raises_wizard_cancelled -v`
Expected: 3 FAILs (current code falls through to numbered-fallback path which calls `input()` → EOFError → returns default; ESC test fails because no `WizardCancelled` defined).

- [ ] **Step 3.3: Implement TTY path in `opencomputer/cli_ui/menu.py`**

Add at top:
```python
from typing import Optional, Any


class WizardCancelled(Exception):
    """ESC pressed in a menu primitive — propagates cleanly through
    nested section handlers without each having to check return values.

    Re-exported from opencomputer.cli_setup.wizard for public callers.
    Lives here in menu.py so menu primitives can raise it without
    importing the wizard module (avoid circular import).
    """
```

Replace the `radiolist()` body to use prompt_toolkit:

```python
def radiolist(
    question: str,
    choices: list[Choice],
    default: int = 0,
    description: Optional[str] = None,
    *,
    _input: Optional[Any] = None,   # injection for tests
    _output: Optional[Any] = None,
) -> int:
    """Single-select arrow-key menu. Returns selected index. Raises
    WizardCancelled on ESC."""
    if not sys.stdin.isatty() and _input is None:
        return _radiolist_numbered_fallback(question, choices, default, description)

    flush_stdin()

    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    from opencomputer.cli_ui.style import (
        ARROW_GLYPH,
        MENU_STYLE,
        RADIO_OFF,
        RADIO_ON,
    )

    cursor = [default]  # mutable index in closure

    def render():
        lines: list[tuple[str, str]] = []
        lines.append(("class:menu.title", question + "\n"))
        lines.append((
            "class:menu.hint",
            "↑↓ navigate  ENTER/SPACE select  ESC cancel\n",
        ))
        if description:
            lines.append(("class:menu.description", f"  {description}\n"))
        lines.append(("", "\n"))
        for i, c in enumerate(choices):
            is_sel = i == cursor[0]
            arrow_class = "class:menu.selected.arrow" if is_sel else "class:"
            arrow = ARROW_GLYPH if is_sel else " "
            row_class = "class:menu.selected" if is_sel else "class:"
            glyph = RADIO_ON if is_sel else RADIO_OFF
            glyph_class = (
                "class:menu.selected.glyph" if is_sel
                else "class:menu.unselected.glyph"
            )
            suffix = f"  ({c.description})" if c.description else ""
            lines.append((arrow_class, f" {arrow} "))
            lines.append((glyph_class, f"({glyph}) "))
            lines.append((row_class, f"{c.label}{suffix}\n"))
        return FormattedText(lines)

    bindings = KeyBindings()

    @bindings.add("up")
    def _up(event):
        cursor[0] = (cursor[0] - 1) % len(choices)

    @bindings.add("down")
    def _down(event):
        cursor[0] = (cursor[0] + 1) % len(choices)

    @bindings.add("enter")
    @bindings.add(" ")
    def _select(event):
        event.app.exit(result=cursor[0])

    @bindings.add("escape")
    @bindings.add("c-c")
    def _cancel(event):
        event.app.exit(exception=WizardCancelled())

    layout = Layout(HSplit([Window(FormattedTextControl(render))]))

    app = Application(
        layout=layout,
        key_bindings=bindings,
        style=MENU_STYLE,
        full_screen=False,
        input=_input,
        output=_output,
    )
    return app.run()
```

- [ ] **Step 3.4: Run TTY tests, verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli_ui_menu.py -v`
Expected: all 7 PASSED (4 numbered-fallback + 3 TTY).

- [ ] **Step 3.5: Commit**

```bash
git add opencomputer/cli_ui/menu.py tests/test_cli_ui_menu.py
git commit -m "feat(cli_ui): prompt_toolkit-based TTY path for radiolist

Arrow-key navigation via prompt_toolkit Application + KeyBindings.
ESC raises WizardCancelled (defined here, re-exported by
cli_setup.wizard in Task 6 to avoid circular import).

Test injection points (_input, _output) let pytest drive the menu via
create_pipe_input + DummyOutput — same pattern prompt_toolkit's own
test suite uses."
```

---

## Task 4: Menu primitive — `checklist` (multi-select)

**Files:**
- Modify: `opencomputer/cli_ui/menu.py` (replace checklist NotImplementedError)
- Modify: `tests/test_cli_ui_menu.py` (add checklist tests)

- [ ] **Step 4.1: Write failing tests**

Append to `tests/test_cli_ui_menu.py`:

```python
def test_checklist_numbered_fallback_returns_selected_indices(monkeypatch):
    from opencomputer.cli_ui.menu import Choice, checklist

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("1,3\n"))

    items = [Choice("Telegram", "telegram"), Choice("Discord", "discord"),
             Choice("Slack", "slack"), Choice("Matrix", "matrix")]
    selected = checklist("Select platforms:", items)
    assert selected == [0, 2]


def test_checklist_numbered_fallback_pre_selected_default(monkeypatch):
    from opencomputer.cli_ui.menu import Choice, checklist

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))

    items = [Choice("A", "a"), Choice("B", "b")]
    selected = checklist("Pick:", items, pre_selected=[1])
    assert selected == [1]


def test_checklist_tty_space_toggles_then_enter_confirms(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, checklist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with create_pipe_input() as pipe_input:
        # Default cursor at 0; SPACE toggles 0; Down; SPACE toggles 1; Enter.
        pipe_input.send_text(" \x1b[B \r")
        selected = checklist(
            "Pick:",
            [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")],
            _input=pipe_input,
            _output=DummyOutput(),
        )
    assert selected == [0, 1]


def test_checklist_tty_esc_raises_wizard_cancelled(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, WizardCancelled, checklist

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x1b")
        with pytest.raises(WizardCancelled):
            checklist(
                "Pick:",
                [Choice("A", "a")],
                _input=pipe_input,
                _output=DummyOutput(),
            )
```

- [ ] **Step 4.2: Run tests, verify RED**

Run: `.venv/bin/python -m pytest tests/test_cli_ui_menu.py -k checklist -v`
Expected: 4 FAILs with `NotImplementedError: checklist lands in Task 4`.

- [ ] **Step 4.3: Implement `checklist` in `opencomputer/cli_ui/menu.py`**

Replace the `def checklist(...): raise NotImplementedError(...)` stub with:

```python
def checklist(
    title: str,
    items: list[Choice],
    pre_selected: Optional[list[int]] = None,
    *,
    _input: Optional[Any] = None,
    _output: Optional[Any] = None,
) -> list[int]:
    """Multi-select menu. Returns sorted list of selected indices."""
    pre_selected = pre_selected or []
    if not sys.stdin.isatty() and _input is None:
        return _checklist_numbered_fallback(title, items, pre_selected)

    flush_stdin()

    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    from opencomputer.cli_ui.style import (
        ARROW_GLYPH,
        CHECK_OFF,
        CHECK_ON,
        MENU_STYLE,
    )

    cursor = [0]
    selected: set[int] = set(pre_selected)

    def render():
        lines = [
            ("class:menu.title", title + "\n"),
            ("class:menu.hint",
             "↑↓ navigate  SPACE toggle  ENTER confirm  ESC cancel\n"),
            ("", "\n"),
        ]
        for i, c in enumerate(items):
            is_cur = i == cursor[0]
            is_sel = i in selected
            arrow = ARROW_GLYPH if is_cur else " "
            arrow_class = "class:menu.selected.arrow" if is_cur else "class:"
            row_class = "class:menu.selected" if is_cur else "class:"
            glyph = CHECK_ON if is_sel else CHECK_OFF
            glyph_class = (
                "class:menu.selected.glyph" if is_sel
                else "class:menu.unselected.glyph"
            )
            suffix = f"  ({c.description})" if c.description else ""
            lines.append((arrow_class, f" {arrow} "))
            lines.append((glyph_class, f"[{glyph}] "))
            lines.append((row_class, f"{c.label}{suffix}\n"))
        return FormattedText(lines)

    bindings = KeyBindings()

    @bindings.add("up")
    def _up(event): cursor[0] = (cursor[0] - 1) % len(items)

    @bindings.add("down")
    def _down(event): cursor[0] = (cursor[0] + 1) % len(items)

    @bindings.add(" ")
    def _toggle(event):
        i = cursor[0]
        if i in selected:
            selected.remove(i)
        else:
            selected.add(i)

    @bindings.add("enter")
    def _confirm(event):
        event.app.exit(result=sorted(selected))

    @bindings.add("escape")
    @bindings.add("c-c")
    def _cancel(event):
        event.app.exit(exception=WizardCancelled())

    layout = Layout(HSplit([Window(FormattedTextControl(render))]))
    app = Application(
        layout=layout, key_bindings=bindings, style=MENU_STYLE,
        full_screen=False, input=_input, output=_output,
    )
    return app.run()


def _checklist_numbered_fallback(
    title: str, items: list[Choice], pre_selected: list[int],
) -> list[int]:
    """Non-TTY multi-select. Reads comma-separated numbers from stdin."""
    print(title)
    for i, c in enumerate(items):
        marker = "[✓]" if i in pre_selected else "[ ]"
        suffix = f"  ({c.description})" if c.description else ""
        print(f"  {marker} {i + 1}. {c.label}{suffix}")
    print()
    pre_str = ",".join(str(i + 1) for i in pre_selected) or "none"
    while True:
        try:
            raw = input(
                f"Numbers comma-separated [default {pre_str}]: "
            ).strip()
        except EOFError:
            return sorted(pre_selected)
        if raw == "":
            return sorted(pre_selected)
        try:
            picks = sorted({int(x.strip()) - 1 for x in raw.split(",") if x.strip()})
        except ValueError:
            print(f"Invalid input '{raw}' — comma-separated numbers only.",
                  file=sys.stderr)
            continue
        if not all(0 <= p < len(items) for p in picks):
            print(f"out of range — only 1-{len(items)} are valid.",
                  file=sys.stderr)
            continue
        return picks
```

- [ ] **Step 4.4: Run, verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli_ui_menu.py -v`
Expected: all 11 PASSED.

- [ ] **Step 4.5: Commit**

```bash
git add opencomputer/cli_ui/menu.py tests/test_cli_ui_menu.py
git commit -m "feat(cli_ui): checklist primitive (multi-select with toggle)

SPACE toggles current row, ENTER confirms with sorted-indices return.
Numbered fallback parses comma-separated numbers."
```

---

## Task 5: Menu primitive — `single_select` (alternative single-select visual)

**Files:**
- Modify: `opencomputer/cli_ui/menu.py`
- Modify: `tests/test_cli_ui_menu.py`

- [ ] **Step 5.1: Write failing tests**

```python
def test_single_select_numbered_fallback_returns_index(monkeypatch):
    from opencomputer.cli_ui.menu import Choice, single_select

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", io.StringIO("3\n"))

    items = [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")]
    idx = single_select("Pick:", items, default=0)
    assert idx == 2


def test_single_select_tty_arrow_then_enter(monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from opencomputer.cli_ui.menu import Choice, single_select

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x1b[B\x1b[B\r")  # Down, Down, Enter
        idx = single_select(
            "Pick:",
            [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")],
            default=0, _input=pipe_input, _output=DummyOutput(),
        )
    assert idx == 2
```

- [ ] **Step 5.2: Run, verify RED**

Run: `.venv/bin/python -m pytest tests/test_cli_ui_menu.py -k single_select -v`
Expected: 2 FAILs (`NotImplementedError: single_select lands in Task 5`).

- [ ] **Step 5.3: Implement `single_select` (visually it's `radiolist` without the `(●)`/`(○)` — Hermes uses it for "Select platforms to configure?" type prompts where the radio glyph would be confusing)**

Replace the stub with:

```python
def single_select(
    title: str,
    items: list[Choice],
    default: int = 0,
    *,
    _input: Optional[Any] = None,
    _output: Optional[Any] = None,
) -> int:
    """Single-select WITHOUT radio glyphs — visual variant of radiolist
    used when a (●)/(○) marker would imply "currently configured" rather
    than "currently focused"."""
    if not sys.stdin.isatty() and _input is None:
        return _single_select_numbered_fallback(title, items, default)

    flush_stdin()

    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    from opencomputer.cli_ui.style import ARROW_GLYPH, MENU_STYLE

    cursor = [default]

    def render():
        lines = [
            ("class:menu.title", title + "\n"),
            ("class:menu.hint",
             "↑↓ navigate  ENTER select  ESC cancel\n"),
            ("", "\n"),
        ]
        for i, c in enumerate(items):
            is_cur = i == cursor[0]
            arrow = ARROW_GLYPH if is_cur else " "
            arrow_class = "class:menu.selected.arrow" if is_cur else "class:"
            row_class = "class:menu.selected" if is_cur else "class:"
            suffix = f"  ({c.description})" if c.description else ""
            lines.append((arrow_class, f" {arrow} "))
            lines.append((row_class, f"{c.label}{suffix}\n"))
        return FormattedText(lines)

    bindings = KeyBindings()

    @bindings.add("up")
    def _up(e): cursor[0] = (cursor[0] - 1) % len(items)

    @bindings.add("down")
    def _down(e): cursor[0] = (cursor[0] + 1) % len(items)

    @bindings.add("enter")
    def _select(e): e.app.exit(result=cursor[0])

    @bindings.add("escape")
    @bindings.add("c-c")
    def _cancel(e): e.app.exit(exception=WizardCancelled())

    layout = Layout(HSplit([Window(FormattedTextControl(render))]))
    app = Application(
        layout=layout, key_bindings=bindings, style=MENU_STYLE,
        full_screen=False, input=_input, output=_output,
    )
    return app.run()


def _single_select_numbered_fallback(
    title: str, items: list[Choice], default: int,
) -> int:
    """Same as _radiolist_numbered_fallback minus the (○)/(●) glyphs."""
    print(title)
    for i, c in enumerate(items):
        marker = "→" if i == default else " "
        suffix = f"  ({c.description})" if c.description else ""
        print(f"  {marker} {i + 1}. {c.label}{suffix}")
    print()
    while True:
        try:
            raw = input(f"Choice [1-{len(items)}, default {default + 1}]: ").strip()
        except EOFError:
            return default
        if raw == "":
            return default
        try:
            n = int(raw)
        except ValueError:
            print(f"Invalid input '{raw}'.", file=sys.stderr)
            continue
        if not 1 <= n <= len(items):
            print(f"out of range.", file=sys.stderr)
            continue
        return n - 1
```

- [ ] **Step 5.4: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli_ui_menu.py -v`
Expected: all 13 PASSED.

- [ ] **Step 5.5: Commit**

```bash
git add opencomputer/cli_ui/menu.py tests/test_cli_ui_menu.py
git commit -m "feat(cli_ui): single_select primitive (radio-glyph-less variant)"
```

---

## Task 6: Wizard sections + WizardCancelled re-export + WizardCtx + SectionResult + WizardSection

**Files:**
- Create: `opencomputer/cli_setup/__init__.py`
- Create: `opencomputer/cli_setup/sections.py`
- Create: `opencomputer/cli_setup/wizard.py` (initial — exception + skeleton run_setup)
- Create: `tests/test_cli_setup_wizard.py`

- [ ] **Step 6.1: Write failing tests for the sections data model**

Create `tests/test_cli_setup_wizard.py`:

```python
"""Tests for cli_setup/wizard.py orchestrator + sections data model."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_section_result_has_four_states():
    from opencomputer.cli_setup.sections import SectionResult
    assert {r.value for r in SectionResult} == {
        "configured", "skipped-keep", "skipped-fresh", "cancelled",
    }


def test_wizard_section_dataclass_fields():
    from opencomputer.cli_setup.sections import WizardSection
    sec = WizardSection(
        key="test", icon="◆", title="Test", description="d",
        handler=lambda ctx: None,
    )
    assert sec.deferred is False
    assert sec.configured_check is None


def test_wizard_ctx_holds_config_path_first_run_flag():
    from opencomputer.cli_setup.sections import WizardCtx
    ctx = WizardCtx(
        config={}, config_path=Path("/tmp/x.yaml"), is_first_run=True,
    )
    assert ctx.is_first_run is True
    assert ctx.quick_mode is False


def test_wizard_cancelled_re_exported_from_wizard_module():
    """Public-facing import path."""
    from opencomputer.cli_setup.wizard import WizardCancelled
    from opencomputer.cli_ui.menu import WizardCancelled as menu_wc
    assert WizardCancelled is menu_wc, "Same exception class, single source"


def test_section_registry_has_eight_entries_with_correct_order():
    from opencomputer.cli_setup.sections import SECTION_REGISTRY
    keys = [s.key for s in SECTION_REGISTRY]
    assert keys == [
        "opencomputer_prior_detect",
        "inference_provider",
        "messaging_platforms",
        "agent_settings",
        "tts_provider",
        "terminal_backend",
        "tools",
        "launchd_service",
    ]


def test_six_sections_are_deferred_two_are_live():
    from opencomputer.cli_setup.sections import SECTION_REGISTRY
    deferred = [s.key for s in SECTION_REGISTRY if s.deferred]
    live = [s.key for s in SECTION_REGISTRY if not s.deferred]
    assert set(deferred) == {
        "opencomputer_prior_detect", "agent_settings", "tts_provider",
        "terminal_backend", "tools", "launchd_service",
    }
    assert set(live) == {"inference_provider", "messaging_platforms"}
```

- [ ] **Step 6.2: Run, verify RED**

Run: `.venv/bin/python -m pytest tests/test_cli_setup_wizard.py -v`
Expected: 6 FAILs (no module).

- [ ] **Step 6.3: Implement `opencomputer/cli_setup/sections.py`**

```python
"""Data model for the wizard's section-driven flow.

Visual + UX modeled after hermes-agent's hermes_cli/setup.py::run_setup_wizard.
Independently re-implemented (no code copied) — see spec § 10 O1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


class SectionResult(Enum):
    CONFIGURED = "configured"
    SKIPPED_KEEP = "skipped-keep"
    SKIPPED_FRESH = "skipped-fresh"
    CANCELLED = "cancelled"


@dataclass
class WizardCtx:
    """Threaded through every section handler. Mutating ``config`` is
    expected; the orchestrator persists the dict to disk after all
    sections run."""

    config: dict
    config_path: Path
    is_first_run: bool
    quick_mode: bool = False
    extra: dict = field(default_factory=dict)


HandlerFn = Callable[[WizardCtx], "SectionResult"]
ConfiguredCheckFn = Callable[[WizardCtx], bool]


@dataclass
class WizardSection:
    """One step in the wizard. Handlers and configured_check both
    receive the WizardCtx."""

    key: str
    icon: str
    title: str
    description: str
    handler: HandlerFn
    configured_check: Optional[ConfiguredCheckFn] = None
    deferred: bool = False
    target_subproject: str = ""  # e.g. "M1", "S1" — populated on deferred sections


def _build_registry() -> list[WizardSection]:
    """Single source of truth for section order. Imports happen here
    (not at module top) so deferred-section subprojects can register
    without circular imports."""
    from opencomputer.cli_setup.section_handlers._deferred import (
        make_deferred_handler,
    )
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        run_inference_provider_section,
        is_inference_provider_configured,
    )
    from opencomputer.cli_setup.section_handlers.messaging_platforms import (
        run_messaging_platforms_section,
        is_messaging_platforms_configured,
    )

    return [
        WizardSection(
            key="opencomputer_prior_detect", icon="◆",
            title="Prior install detection",
            description="Detect existing OpenClaw / Hermes / OpenComputer data and offer to migrate.",
            handler=make_deferred_handler("M1"), deferred=True, target_subproject="M1",
        ),
        WizardSection(
            key="inference_provider", icon="◆",
            title="Inference Provider",
            description=(
                "Choose how to connect to your main chat model.\n"
                "Guide: https://github.com/sakshamzip2-sys/opencomputer#providers"
            ),
            handler=run_inference_provider_section,
            configured_check=is_inference_provider_configured,
        ),
        WizardSection(
            key="messaging_platforms", icon="◆",
            title="Messaging Platforms",
            description=(
                "Connect to messaging platforms to chat with OpenComputer from anywhere.\n"
                "Toggle with Space, confirm with Enter."
            ),
            handler=run_messaging_platforms_section,
            configured_check=is_messaging_platforms_configured,
        ),
        WizardSection(
            key="agent_settings", icon="◆", title="Agent settings",
            description="Max iterations, compression threshold, session reset.",
            handler=make_deferred_handler("S1"), deferred=True, target_subproject="S1",
        ),
        WizardSection(
            key="tts_provider", icon="◆", title="TTS provider",
            description="Voice output: NeutTTS / KittenTTS / eSpeak-NG / ElevenLabs / OpenAI TTS.",
            handler=make_deferred_handler("S2"), deferred=True, target_subproject="S2",
        ),
        WizardSection(
            key="terminal_backend", icon="◆", title="Terminal backend",
            description="Sandboxed shell: Apptainer / Docker / native.",
            handler=make_deferred_handler("S3"), deferred=True, target_subproject="S3",
        ),
        WizardSection(
            key="tools", icon="◆", title="Tools",
            description="Optional tool plugins.",
            handler=make_deferred_handler("S4"), deferred=True, target_subproject="S4",
        ),
        WizardSection(
            key="launchd_service", icon="◆", title="Launchd service",
            description="Run gateway as a launchd service (starts on boot).",
            handler=make_deferred_handler("S5"), deferred=True, target_subproject="S5",
        ),
    ]


SECTION_REGISTRY: list[WizardSection] = _build_registry()
```

- [ ] **Step 6.4: Implement `opencomputer/cli_setup/wizard.py` skeleton (full orchestrator lands in Task 7; this commit just re-exports `WizardCancelled`)**

```python
"""Wizard orchestrator — top-level entry point ``run_setup``.

Re-exports WizardCancelled from cli_ui.menu (single source) so callers
can `from opencomputer.cli_setup.wizard import WizardCancelled` without
having to know that the exception physically lives in the menu module
(it lives there to avoid a circular import: menu raises it,
section handlers catch it).
"""
from __future__ import annotations

from opencomputer.cli_ui.menu import WizardCancelled

__all__ = ["WizardCancelled", "run_setup"]


def run_setup(*, quick: bool = False) -> int:
    """Top-level wizard entry. Stub — full orchestrator lands in Task 7."""
    raise NotImplementedError("run_setup orchestrator lands in Task 7")
```

- [ ] **Step 6.5: Implement `opencomputer/cli_setup/__init__.py`**

```python
"""Hermes-modeled section-driven setup wizard."""
from opencomputer.cli_setup.sections import (
    SECTION_REGISTRY,
    SectionResult,
    WizardCtx,
    WizardSection,
)
from opencomputer.cli_setup.wizard import WizardCancelled, run_setup

__all__ = [
    "SECTION_REGISTRY",
    "SectionResult",
    "WizardCancelled",
    "WizardCtx",
    "WizardSection",
    "run_setup",
]
```

- [ ] **Step 6.6: Defer test for handlers (sections.py imports them) — create stub handler modules so imports don't fail**

Create `opencomputer/cli_setup/section_handlers/__init__.py`:
```python
"""Section handlers."""
```

Create `opencomputer/cli_setup/section_handlers/_deferred.py`:
```python
"""Deferred section placeholder — prints a stub that names the
follow-up sub-project and returns SKIPPED_FRESH."""
from __future__ import annotations

from typing import Callable

from opencomputer.cli_setup.sections import SectionResult, WizardCtx


def make_deferred_handler(target_subproject: str) -> Callable[[WizardCtx], SectionResult]:
    def handler(ctx: WizardCtx) -> SectionResult:
        print(f"  (deferred — coming in sub-project {target_subproject})")
        return SectionResult.SKIPPED_FRESH
    return handler
```

Create `opencomputer/cli_setup/section_handlers/inference_provider.py` (stub for now — full impl in Task 9):
```python
"""Inference provider section. Full impl in Task 9."""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx


def is_inference_provider_configured(ctx: WizardCtx) -> bool:
    model = ctx.config.get("model") or {}
    provider = model.get("provider")
    return bool(provider) and provider != "none"


def run_inference_provider_section(ctx: WizardCtx) -> SectionResult:
    raise NotImplementedError("inference_provider handler lands in Task 9")
```

Create `opencomputer/cli_setup/section_handlers/messaging_platforms.py`:
```python
"""Messaging platforms section. Full impl in Task 10."""
from __future__ import annotations

from opencomputer.cli_setup.sections import SectionResult, WizardCtx


def is_messaging_platforms_configured(ctx: WizardCtx) -> bool:
    # Configured if any channel adapter env var is set OR config.gateway.platforms is non-empty
    gw = ctx.config.get("gateway") or {}
    return bool(gw.get("platforms"))


def run_messaging_platforms_section(ctx: WizardCtx) -> SectionResult:
    raise NotImplementedError("messaging_platforms handler lands in Task 10")
```

- [ ] **Step 6.7: Run, verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli_setup_wizard.py -v`
Expected: 6 PASSED.

- [ ] **Step 6.8: Commit**

```bash
git add opencomputer/cli_setup/ tests/test_cli_setup_wizard.py
git commit -m "feat(cli_setup): wizard sections data model + section handler stubs

WizardSection, SectionResult, WizardCtx, SECTION_REGISTRY (8 sections,
2 live + 6 deferred). Handler files stubbed with NotImplementedError;
real impls land in Tasks 9 and 10.

WizardCancelled re-exported from cli_setup.wizard so the public-facing
import path matches the spec; physical home stays in cli_ui.menu to
avoid circular imports."
```

---

## Task 7: Wizard orchestrator (`run_setup`)

**Files:**
- Modify: `opencomputer/cli_setup/wizard.py`
- Modify: `tests/test_cli_setup_wizard.py`

- [ ] **Step 7.1: Write failing tests**

Append to `tests/test_cli_setup_wizard.py`:

```python
def test_run_setup_iterates_all_sections_in_order(monkeypatch, tmp_path, capsys):
    """Each section's handler is invoked once, in registry order."""
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup.sections import SectionResult, WizardSection
    from opencomputer.cli_setup.wizard import run_setup

    calls: list[str] = []

    def mk_handler(key):
        def h(ctx):
            calls.append(key)
            return SectionResult.SKIPPED_FRESH
        return h

    fake_registry = [
        WizardSection(key=k, icon="◆", title=k, description="d",
                      handler=mk_handler(k))
        for k in ["alpha", "beta", "gamma"]
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)

    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "opencomputer.cli_setup.wizard._resolve_config_path",
        lambda: config_path,
    )

    rc = run_setup()
    assert rc == 0
    assert calls == ["alpha", "beta", "gamma"]


def test_run_setup_skips_deferred_sections_without_calling_handler(
    monkeypatch, tmp_path, capsys
):
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup.sections import SectionResult, WizardSection
    from opencomputer.cli_setup.wizard import run_setup

    bad_called = []
    def bad(ctx):
        bad_called.append(True)
        return SectionResult.CONFIGURED

    fake_registry = [
        WizardSection(key="x", icon="◆", title="X", description="d",
                      handler=bad, deferred=True, target_subproject="M9"),
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)
    monkeypatch.setattr(
        "opencomputer.cli_setup.wizard._resolve_config_path",
        lambda: tmp_path / "config.yaml",
    )

    rc = run_setup()
    assert rc == 0
    assert bad_called == []
    out = capsys.readouterr().out
    assert "M9" in out


def test_run_setup_writes_config_after_all_sections(monkeypatch, tmp_path):
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup.sections import SectionResult, WizardSection
    from opencomputer.cli_setup.wizard import run_setup

    def mutating(ctx):
        ctx.config["model"] = {"provider": "anthropic"}
        return SectionResult.CONFIGURED

    fake_registry = [
        WizardSection(key="m", icon="◆", title="M", description="d",
                      handler=mutating),
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "opencomputer.cli_setup.wizard._resolve_config_path",
        lambda: config_path,
    )

    rc = run_setup()
    assert rc == 0
    import yaml
    written = yaml.safe_load(config_path.read_text())
    assert written["model"]["provider"] == "anthropic"


def test_run_setup_configured_check_offers_keep_reconfigure_skip(
    monkeypatch, tmp_path
):
    """When configured_check returns True, a 3-option radiolist gates the handler."""
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup import wizard as wiz
    from opencomputer.cli_setup.sections import SectionResult, WizardSection

    handler_called = []
    def h(ctx):
        handler_called.append(True)
        return SectionResult.CONFIGURED

    fake_registry = [
        WizardSection(
            key="c", icon="◆", title="C", description="d", handler=h,
            configured_check=lambda ctx: True,
        ),
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)
    monkeypatch.setattr(wiz, "_resolve_config_path", lambda: tmp_path / "config.yaml")

    # Mock radiolist to return index 0 = "Keep current"
    from opencomputer.cli_setup import wizard as wiz_mod
    monkeypatch.setattr(wiz_mod, "radiolist", lambda *a, **kw: 0)

    rc = wiz.run_setup()
    assert rc == 0
    assert handler_called == [], "Keep-current must NOT call handler"


def test_run_setup_configured_check_reconfigure_calls_handler(
    monkeypatch, tmp_path
):
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup import wizard as wiz
    from opencomputer.cli_setup.sections import SectionResult, WizardSection

    called = []
    def h(ctx):
        called.append(True)
        return SectionResult.CONFIGURED

    fake_registry = [
        WizardSection(
            key="c", icon="◆", title="C", description="d", handler=h,
            configured_check=lambda ctx: True,
        ),
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)
    monkeypatch.setattr(wiz, "_resolve_config_path", lambda: tmp_path / "config.yaml")

    # idx 1 = "Reconfigure"
    monkeypatch.setattr("opencomputer.cli_setup.wizard.radiolist",
                        lambda *a, **kw: 1)

    rc = wiz.run_setup()
    assert rc == 0
    assert called == [True]


def test_run_setup_esc_during_section_returns_nonzero(monkeypatch, tmp_path):
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup.sections import WizardSection
    from opencomputer.cli_setup.wizard import WizardCancelled, run_setup

    def cancelling(ctx):
        raise WizardCancelled()

    fake_registry = [
        WizardSection(key="x", icon="◆", title="X", description="d",
                      handler=cancelling),
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)
    monkeypatch.setattr(
        "opencomputer.cli_setup.wizard._resolve_config_path",
        lambda: tmp_path / "config.yaml",
    )

    rc = run_setup()
    assert rc == 1, "ESC during a section returns non-zero"
```

- [ ] **Step 7.2: Run, verify RED**

Run: `.venv/bin/python -m pytest tests/test_cli_setup_wizard.py -k run_setup -v`
Expected: 6 FAILs with `NotImplementedError("run_setup orchestrator lands in Task 7")`.

- [ ] **Step 7.3: Replace `run_setup` body in `opencomputer/cli_setup/wizard.py`**

```python
"""Wizard orchestrator — section-driven flow.

Visual + UX modeled after hermes-agent's hermes_cli/setup.py::run_setup_wizard.
Independently re-implemented (no code copied) — see spec § 10 O1.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.panel import Panel

from opencomputer.cli_setup.sections import (
    SECTION_REGISTRY,
    SectionResult,
    WizardCtx,
)
from opencomputer.cli_ui.menu import Choice, WizardCancelled, radiolist

__all__ = ["WizardCancelled", "run_setup"]

_console = Console()


def _resolve_config_path() -> Path:
    """Return path to the active profile's config.yaml.

    Delegates to opencomputer.agent.config_store.config_file_path() —
    the canonical resolver that already accounts for OPENCOMPUTER_HOME
    + per-profile overrides. Wrapper exists so tests can monkeypatch
    just this name without touching every config_store consumer.
    """
    from opencomputer.agent.config_store import config_file_path
    return config_file_path()


def _load_config(path: Path) -> tuple[dict, bool]:
    """Returns (config_dict, is_first_run)."""
    if not path.exists():
        return {}, True
    try:
        return yaml.safe_load(path.read_text()) or {}, False
    except Exception:  # noqa: BLE001 — never crash on bad YAML
        return {}, True


def _save_config(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False))


def _print_header() -> None:
    _console.print(Panel(
        "Let's configure your OpenComputer installation.\n"
        "Press Ctrl+C at any time to exit.",
        title="✦ OpenComputer Setup Wizard",
        border_style="magenta",
    ))


def _print_section_header(icon: str, title: str, description: str) -> None:
    _console.print(f"\n[bold cyan]{icon} {title}[/bold cyan]")
    for line in description.splitlines():
        _console.print(f"  [dim]{line}[/dim]")


def _print_section_footer(result: SectionResult) -> None:
    msg = {
        SectionResult.CONFIGURED: "[green]✓ Configured[/green]",
        SectionResult.SKIPPED_KEEP: "[dim]Skipped (keeping current)[/dim]",
        SectionResult.SKIPPED_FRESH: "[dim]Skipped[/dim]",
        SectionResult.CANCELLED: "[red]✗ Cancelled[/red]",
    }[result]
    _console.print(f"  {msg}")


def _gate_configured_section(ctx: WizardCtx, section_title: str) -> Optional[SectionResult]:
    """When the section reports configured, ask keep / reconfigure / skip.
    Returns:
      - SectionResult.SKIPPED_KEEP / SectionResult.SKIPPED_FRESH if user chose
        not to invoke the handler
      - None if user chose to reconfigure (caller should invoke handler)
    """
    choices = [
        Choice("Keep current", "keep"),
        Choice("Reconfigure", "reconfigure"),
        Choice("Skip", "skip"),
    ]
    idx = radiolist(
        f"{section_title} is already configured — what would you like to do?",
        choices, default=0,
    )
    if idx == 0:
        return SectionResult.SKIPPED_KEEP
    if idx == 1:
        return None  # caller invokes handler
    return SectionResult.SKIPPED_FRESH


def _safe_configured_check(section, ctx) -> bool:
    """Call section.configured_check defensively. Bug in handler must not crash wizard."""
    if section.configured_check is None:
        return False
    try:
        return bool(section.configured_check(ctx))
    except Exception as exc:  # noqa: BLE001
        _console.print(
            f"  [yellow]⚠ configured_check raised {type(exc).__name__} — "
            f"treating as 'not configured'[/yellow]"
        )
        return False


def run_setup(*, quick: bool = False) -> int:
    """Top-level wizard entry. Returns exit code (0 = ok, 1 = cancelled,
    2 = uncaught error). Always returns; never raises."""
    config_path = _resolve_config_path()
    config, is_first_run = _load_config(config_path)

    ctx = WizardCtx(
        config=config,
        config_path=config_path,
        is_first_run=is_first_run,
        quick_mode=quick,
    )

    _print_header()

    try:
        for section in SECTION_REGISTRY:
            _print_section_header(section.icon, section.title, section.description)

            if section.deferred:
                section.handler(ctx)  # prints stub line, returns SKIPPED_FRESH
                _print_section_footer(SectionResult.SKIPPED_FRESH)
                continue

            try:
                if _safe_configured_check(section, ctx):
                    gated = _gate_configured_section(ctx, section.title)
                    if gated is not None:
                        _print_section_footer(gated)
                        continue

                result = section.handler(ctx)
                _print_section_footer(result)
            except WizardCancelled:
                _print_section_footer(SectionResult.CANCELLED)
                _console.print("\n[red]Setup cancelled.[/red] Run `oc setup` again to retry.")
                return 1
    except KeyboardInterrupt:
        _console.print("\n[red]Setup interrupted (Ctrl+C).[/red] Run `oc setup` again to retry.")
        return 1

    _save_config(config_path, ctx.config)
    _console.print("\n[green]✓ Setup complete.[/green] Run `oc chat` to start.")
    return 0
```

- [ ] **Step 7.4: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli_setup_wizard.py -v`
Expected: 12 PASSED.

- [ ] **Step 7.5: Commit**

```bash
git add opencomputer/cli_setup/wizard.py tests/test_cli_setup_wizard.py
git commit -m "feat(cli_setup): section-driven wizard orchestrator

run_setup walks SECTION_REGISTRY, prints headers, gates already-
configured sections through a 3-option radiolist (keep/reconfigure/
skip), invokes handlers, persists config. ESC propagates as
WizardCancelled and yields rc=1.

Visual + UX modeled after hermes-agent's run_setup_wizard.
Independently re-implemented on prompt_toolkit + rich."
```

---

## Task 8: `inference_provider` section handler (full impl)

**Files:**
- Modify: `opencomputer/cli_setup/section_handlers/inference_provider.py`
- Create: `tests/test_cli_setup_section_inference_provider.py`

- [ ] **Step 8.1: Write failing tests**

```python
"""Tests for the inference-provider wizard section."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from opencomputer.cli_setup.sections import WizardCtx
    return WizardCtx(
        config=config or {},
        config_path=tmp_path / "config.yaml",
        is_first_run=True,
    )


def test_run_lists_all_discovered_providers_plus_custom(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    fake_providers = [
        {"name": "anthropic", "label": "Anthropic", "description": "Claude"},
        {"name": "openai", "label": "OpenAI", "description": "GPT-4"},
    ]
    monkeypatch.setattr(ip, "_discover_providers", lambda: fake_providers)

    captured_choices = []
    def fake_radiolist(question, choices, default=0, description=None, **kw):
        captured_choices.extend(choices)
        return 0  # pick first
    monkeypatch.setattr(ip, "radiolist", fake_radiolist)
    monkeypatch.setattr(ip, "_invoke_provider_setup",
                         lambda name, ctx: True)

    ctx = _make_ctx(tmp_path)
    ip.run_inference_provider_section(ctx)

    labels = [c.label for c in captured_choices]
    assert "Anthropic" in labels and "OpenAI" in labels
    assert "Custom endpoint (enter URL manually)" in labels
    assert "Leave unchanged" in labels


def test_run_writes_provider_to_config_on_selection(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {"name": "anthropic", "label": "Anthropic", "description": "Claude"},
    ])
    monkeypatch.setattr(ip, "radiolist", lambda *a, **kw: 0)  # pick anthropic
    monkeypatch.setattr(ip, "_invoke_provider_setup",
                         lambda name, ctx: True)

    ctx = _make_ctx(tmp_path)
    result = ip.run_inference_provider_section(ctx)

    assert result == SectionResult.CONFIGURED
    assert ctx.config["model"]["provider"] == "anthropic"


def test_run_leave_unchanged_returns_skipped_keep(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {"name": "anthropic", "label": "Anthropic", "description": "Claude"},
    ])
    # Choices: [Anthropic, Custom endpoint, Leave unchanged] → idx 2
    monkeypatch.setattr(ip, "radiolist", lambda *a, **kw: 2)

    ctx = _make_ctx(tmp_path, config={"model": {"provider": "anthropic"}})
    result = ip.run_inference_provider_section(ctx)
    assert result == SectionResult.SKIPPED_KEEP


def test_is_configured_returns_true_when_provider_set(tmp_path):
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        is_inference_provider_configured,
    )
    ctx = _make_ctx(tmp_path, config={"model": {"provider": "anthropic"}})
    assert is_inference_provider_configured(ctx) is True


def test_is_configured_returns_false_for_none_provider(tmp_path):
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        is_inference_provider_configured,
    )
    ctx = _make_ctx(tmp_path, config={"model": {"provider": "none"}})
    assert is_inference_provider_configured(ctx) is False
```

- [ ] **Step 8.2: Run, verify RED**

Run: `.venv/bin/python -m pytest tests/test_cli_setup_section_inference_provider.py -v`
Expected: 5 FAILs (mostly `NotImplementedError` + missing helpers).

- [ ] **Step 8.3: Replace `inference_provider.py` with full impl**

```python
"""Inference provider section handler.

Discovers provider plugins via opencomputer.plugins.discovery; lets
user pick one via radiolist; invokes the chosen plugin's setup hook.
"""
from __future__ import annotations

from typing import Any

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist


def is_inference_provider_configured(ctx: WizardCtx) -> bool:
    model = ctx.config.get("model") or {}
    provider = model.get("provider")
    return bool(provider) and provider != "none"


def _discover_providers() -> list[dict[str, Any]]:
    """Return list of {'name', 'label', 'description'} for every provider
    plugin's manifest.setup.providers entry."""
    try:
        from opencomputer.plugins.discovery import discover, standard_search_paths
        candidates = discover(standard_search_paths())
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for cand in candidates:
        setup = cand.manifest.setup
        if setup is None:
            continue
        for prov in setup.providers:
            out.append({
                "name": prov.name,
                "label": getattr(prov, "label", prov.name.title()),
                "description": getattr(prov, "description", "") or "",
            })
    return out


def _invoke_provider_setup(name: str, ctx: WizardCtx) -> bool:
    """Call the provider plugin's setup hook (if any). Returns True on
    success. For now this delegates to legacy `_prompt_api_key` behavior
    if no setup hook is defined."""
    try:
        from opencomputer.plugins.discovery import discover, standard_search_paths
        for cand in discover(standard_search_paths()):
            setup = cand.manifest.setup
            if setup is None:
                continue
            for prov in setup.providers:
                if prov.name == name:
                    # Update config minimally — primary env var name + provider
                    env_var = (prov.env_vars or [None])[0]
                    ctx.config.setdefault("model", {})
                    ctx.config["model"]["provider"] = name
                    if env_var:
                        ctx.config["model"]["api_key_env"] = env_var
                    return True
    except Exception:  # noqa: BLE001
        pass
    # Fallback: minimum config write
    ctx.config.setdefault("model", {})
    ctx.config["model"]["provider"] = name
    return True


def run_inference_provider_section(ctx: WizardCtx) -> SectionResult:
    providers = _discover_providers()
    choices: list[Choice] = []
    for p in providers:
        choices.append(Choice(
            label=p["label"], value=p["name"],
            description=p["description"] or None,
        ))
    choices.append(Choice(
        label="Custom endpoint (enter URL manually)", value="__custom__",
        description="Manually configure base_url + api_key_env",
    ))
    choices.append(Choice(label="Leave unchanged", value="__leave__"))

    idx = radiolist("Select provider:", choices, default=0)
    chosen = choices[idx].value

    if chosen == "__leave__":
        return SectionResult.SKIPPED_KEEP

    if chosen == "__custom__":
        # Leave detailed custom-endpoint UI to a follow-up; minimal
        # acceptance for this PR is to mark it pending.
        ctx.config.setdefault("model", {})
        ctx.config["model"]["provider"] = "custom"
        return SectionResult.CONFIGURED

    # Real provider
    ok = _invoke_provider_setup(str(chosen), ctx)
    return SectionResult.CONFIGURED if ok else SectionResult.SKIPPED_FRESH
```

- [ ] **Step 8.4: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli_setup_section_inference_provider.py -v`
Expected: 5 PASSED.

- [ ] **Step 8.5: Commit**

```bash
git add opencomputer/cli_setup/section_handlers/inference_provider.py \
        tests/test_cli_setup_section_inference_provider.py
git commit -m "feat(cli_setup): inference_provider section handler

Discovers provider plugins via opencomputer.plugins.discovery, presents
a radiolist with [providers..., Custom endpoint, Leave unchanged],
invokes the chosen plugin's setup hook (or writes minimal config for
__custom__ / __leave__).

Configured-check returns True when model.provider is set and not 'none'."
```

---

## Task 9: `messaging_platforms` section handler (full impl)

**Files:**
- Modify: `opencomputer/cli_setup/section_handlers/messaging_platforms.py`
- Create: `tests/test_cli_setup_section_messaging_platforms.py`

- [ ] **Step 9.1: Write failing tests**

```python
"""Tests for the messaging-platforms wizard section."""
from __future__ import annotations

from pathlib import Path

import pytest


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from opencomputer.cli_setup.sections import WizardCtx
    return WizardCtx(
        config=config or {},
        config_path=tmp_path / "config.yaml",
        is_first_run=True,
    )


def test_skip_branch_returns_skipped_fresh(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp
    from opencomputer.cli_setup.sections import SectionResult

    # First radiolist: 1 = Skip
    monkeypatch.setattr(mp, "radiolist", lambda *a, **kw: 1)

    ctx = _make_ctx(tmp_path)
    result = mp.run_messaging_platforms_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH


def test_setup_now_branch_calls_checklist_and_invokes_per_platform(
    monkeypatch, tmp_path
):
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp
    from opencomputer.cli_setup.sections import SectionResult

    fake_platforms = [
        {"name": "telegram", "label": "Telegram", "configured": False},
        {"name": "discord", "label": "Discord", "configured": False},
    ]
    monkeypatch.setattr(mp, "_discover_platforms", lambda: fake_platforms)

    monkeypatch.setattr(mp, "radiolist", lambda *a, **kw: 0)  # set up now
    monkeypatch.setattr(mp, "checklist", lambda *a, **kw: [0, 1])  # both

    invocations: list[str] = []
    def fake_invoke(name, ctx):
        invocations.append(name)
        return True
    monkeypatch.setattr(mp, "_invoke_platform_setup", fake_invoke)

    ctx = _make_ctx(tmp_path)
    result = mp.run_messaging_platforms_section(ctx)

    assert result == SectionResult.CONFIGURED
    assert invocations == ["telegram", "discord"]


def test_no_platforms_selected_returns_skipped_fresh(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(mp, "_discover_platforms", lambda: [
        {"name": "telegram", "label": "Telegram", "configured": False},
    ])
    monkeypatch.setattr(mp, "radiolist", lambda *a, **kw: 0)
    monkeypatch.setattr(mp, "checklist", lambda *a, **kw: [])

    ctx = _make_ctx(tmp_path)
    result = mp.run_messaging_platforms_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH


def test_is_messaging_platforms_configured(tmp_path):
    from opencomputer.cli_setup.section_handlers.messaging_platforms import (
        is_messaging_platforms_configured,
    )

    empty = _make_ctx(tmp_path)
    assert is_messaging_platforms_configured(empty) is False

    with_platform = _make_ctx(tmp_path,
        config={"gateway": {"platforms": ["telegram"]}})
    assert is_messaging_platforms_configured(with_platform) is True
```

- [ ] **Step 9.2: Run, verify RED**

Run: `.venv/bin/python -m pytest tests/test_cli_setup_section_messaging_platforms.py -v`
Expected: 4 FAILs (NotImplementedError + missing helpers).

- [ ] **Step 9.3: Replace `messaging_platforms.py` with full impl**

```python
"""Messaging platforms section handler.

Two-step Hermes flow:
  1. radiolist "Connect a messaging platform?" → [Set up now / Skip]
  2. checklist "Select platforms to configure:" → list of channel-kind plugins
"""
from __future__ import annotations

from typing import Any

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, checklist, radiolist


def is_messaging_platforms_configured(ctx: WizardCtx) -> bool:
    gw = ctx.config.get("gateway") or {}
    return bool(gw.get("platforms"))


def _discover_platforms() -> list[dict[str, Any]]:
    """Return list of {'name', 'label', 'configured'} for each channel-kind
    plugin discovered via opencomputer.plugins.discovery."""
    try:
        from opencomputer.plugins.discovery import discover, standard_search_paths
        candidates = discover(standard_search_paths())
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for cand in candidates:
        kind = getattr(cand.manifest, "kind", None)
        if kind not in ("channel", "mixed"):
            continue
        name = cand.manifest.id
        label = getattr(cand.manifest, "label", None) or name.title()
        out.append({
            "name": name,
            "label": label,
            "configured": False,  # detailed check left to follow-up
        })
    return out


def _invoke_platform_setup(name: str, ctx: WizardCtx) -> bool:
    """Stub — calls plugin's setup hook if available. For this PR we
    record the platform name in config; per-platform credential prompts
    (Telegram bot token, Discord token, etc.) are deferred to S5/M2."""
    ctx.config.setdefault("gateway", {})
    ctx.config["gateway"].setdefault("platforms", [])
    if name not in ctx.config["gateway"]["platforms"]:
        ctx.config["gateway"]["platforms"].append(name)
    return True


def run_messaging_platforms_section(ctx: WizardCtx) -> SectionResult:
    gate_choices = [
        Choice("Set up messaging now (recommended)", "now"),
        Choice("Skip — set up later with `oc setup gateway`", "skip"),
    ]
    gate_idx = radiolist(
        "Connect a messaging platform? (Telegram, Discord, etc.)",
        gate_choices, default=0,
    )
    if gate_idx == 1:
        return SectionResult.SKIPPED_FRESH

    platforms = _discover_platforms()
    if not platforms:
        return SectionResult.SKIPPED_FRESH

    items = [
        Choice(
            label=p["label"],
            value=p["name"],
            description="(configured)" if p["configured"] else "(not configured)",
        )
        for p in platforms
    ]
    selected = checklist("Select platforms to configure:", items)
    if not selected:
        return SectionResult.SKIPPED_FRESH

    for idx in selected:
        _invoke_platform_setup(platforms[idx]["name"], ctx)

    return SectionResult.CONFIGURED
```

- [ ] **Step 9.4: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli_setup_section_messaging_platforms.py -v`
Expected: 4 PASSED.

- [ ] **Step 9.5: Commit**

```bash
git add opencomputer/cli_setup/section_handlers/messaging_platforms.py \
        tests/test_cli_setup_section_messaging_platforms.py
git commit -m "feat(cli_setup): messaging_platforms section handler

Two-step gate: 'set up now / skip', then multi-select checklist of
channel-kind plugins. Per-platform credential prompts (bot tokens,
allowlists, home channels) deferred to S5/M2 — this section only
records platform names in config.gateway.platforms."
```

---

## Task 10: ASCII art + version label helpers

**Files:**
- Create: `opencomputer/cli_banner_art.py`
- Create: `tests/test_cli_banner.py` (initial — version label only)

- [ ] **Step 10.1: Write failing test for version label helper**

Create `tests/test_cli_banner.py`:

```python
"""Tests for cli_banner.py — banner assembly + helpers."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_format_banner_version_label_includes_version_string():
    from opencomputer import __version__
    from opencomputer.cli_banner import format_banner_version_label

    label = format_banner_version_label()
    assert __version__ in label
    assert "OpenComputer" in label


def test_format_banner_version_label_includes_git_sha_when_available(monkeypatch):
    from opencomputer.cli_banner import format_banner_version_label

    monkeypatch.setattr(
        "opencomputer.cli_banner._git_short_sha", lambda: "deadbeef"
    )
    assert "deadbeef" in format_banner_version_label()


def test_format_banner_version_label_omits_git_sha_when_unavailable(monkeypatch):
    from opencomputer.cli_banner import format_banner_version_label

    monkeypatch.setattr("opencomputer.cli_banner._git_short_sha", lambda: None)
    label = format_banner_version_label()
    # Just ensure no crash and no literal "None"
    assert "None" not in label


def test_ascii_art_constants_exist():
    from opencomputer.cli_banner_art import OPENCOMPUTER_LOGO, SIDE_GLYPH
    assert isinstance(OPENCOMPUTER_LOGO, str)
    assert "OPENCOMPUTER" in OPENCOMPUTER_LOGO.upper()
    assert isinstance(SIDE_GLYPH, str)
    assert len(SIDE_GLYPH.splitlines()) >= 6, "Side glyph is at least 6 lines"
```

- [ ] **Step 10.2: Run, verify RED**

Run: `.venv/bin/python -m pytest tests/test_cli_banner.py -v`
Expected: 4 FAILs (no module).

- [ ] **Step 10.3: Implement `opencomputer/cli_banner_art.py`**

```python
"""ASCII art constants for the welcome banner.

Visual register modeled after hermes-agent's banner.py (HERMES-AGENT
art) — independently re-drawn (no glyphs copied). Logo uses figlet
'slant' font with tweaks; side glyph is a simple geometric mark.
"""
from __future__ import annotations

# Logo: 7-row figlet 'slant' rendering of "OPENCOMPUTER"
OPENCOMPUTER_LOGO = r"""
   ____  ____  _________   __ __________  __  _____  __  ____________  ____
  / __ \/ __ \/ ____/ __ \ / // ____/ __ \/  |/  / /_/ / / /_  __/ __ \/ __ \
 / / / / /_/ / __/ / / / // // /   / / / / /|_/ / __/ / / / / / / /_/ / /_/ /
/ /_/ / ____/ /___/ /| | // // /___/ /_/ / /  / / /_/ /_/ / / / / _, _/ _, _/
\____/_/   /_____/_/ |_|//_(_)____/\____/_/  /_/\__/\____/ /_/ /_/ |_/_/ |_|
"""

# Side glyph: 12-line abstract mark suggesting layered geometry
SIDE_GLYPH = r"""
        .::::::.
      .::::::::::.
     :::: OC ::::
    :::::::::::::::
   :::      :::::
   :::      :::::
   :::      :::::
    :::::::::::::::
     :::::::::::
      .::::::::::.
        .::::::.
"""
```

- [ ] **Step 10.4: Implement `opencomputer/cli_banner.py` (initial — version label only)**

```python
"""OpenComputer welcome banner.

Visual + structure modeled after hermes-agent's banner.py.
Independently re-implemented on rich (no code copied).

Public API:
  - build_welcome_banner(console, model, cwd, *, session_id, home) -> None
  - format_banner_version_label() -> str
  - get_available_skills() -> dict[str, list[str]]
  - get_available_tools() -> dict[str, list[str]]
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from opencomputer import __version__

__all__ = [
    "build_welcome_banner",
    "format_banner_version_label",
    "get_available_skills",
    "get_available_tools",
]


def _git_short_sha() -> Optional[str]:
    """Return 7-char git SHA of HEAD, or None if not in a git repo."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"],
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).parent,
            text=True,
            timeout=2,
        ).strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def format_banner_version_label() -> str:
    """`OpenComputer v0.1.0 · sha`."""
    sha = _git_short_sha()
    if sha:
        return f"OpenComputer v{__version__} · {sha}"
    return f"OpenComputer v{__version__}"


def get_available_skills() -> dict[str, list[str]]:
    raise NotImplementedError("Lands in Task 11")


def get_available_tools() -> dict[str, list[str]]:
    raise NotImplementedError("Lands in Task 11")


def build_welcome_banner(*args, **kwargs) -> None:
    raise NotImplementedError("Lands in Task 12")
```

- [ ] **Step 10.5: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli_banner.py -v`
Expected: 4 PASSED.

- [ ] **Step 10.6: Commit**

```bash
git add opencomputer/cli_banner_art.py opencomputer/cli_banner.py \
        tests/test_cli_banner.py
git commit -m "feat(cli_banner): ASCII art constants + version label helper

OPENCOMPUTER_LOGO (figlet 'slant') + SIDE_GLYPH (abstract mark) +
format_banner_version_label which combines __version__ + 7-char git
sha when in a git checkout. get_available_skills / get_available_tools
/ build_welcome_banner stubbed for next tasks."
```

---

## Task 11: Skill / tool discovery helpers

**Files:**
- Modify: `opencomputer/cli_banner.py`
- Modify: `tests/test_cli_banner.py`

- [ ] **Step 11.1: Write failing tests**

Append to `tests/test_cli_banner.py`:

```python
def test_get_available_skills_walks_skill_dirs(monkeypatch, tmp_path):
    from opencomputer.cli_banner import get_available_skills

    # Set up a fake skill tree
    (tmp_path / "coding" / "edit-skill").mkdir(parents=True)
    (tmp_path / "coding" / "edit-skill" / "SKILL.md").write_text("# Edit\n")
    (tmp_path / "coding" / "review-skill").mkdir()
    (tmp_path / "coding" / "review-skill" / "SKILL.md").write_text("# Review\n")
    (tmp_path / "research" / "arxiv").mkdir(parents=True)
    (tmp_path / "research" / "arxiv" / "SKILL.md").write_text("# arxiv\n")

    monkeypatch.setattr(
        "opencomputer.cli_banner._skill_search_paths",
        lambda: [tmp_path],
    )

    grouped = get_available_skills()
    assert sorted(grouped["coding"]) == ["edit-skill", "review-skill"]
    assert grouped["research"] == ["arxiv"]


def test_get_available_skills_dedupes_across_search_paths(
    monkeypatch, tmp_path
):
    from opencomputer.cli_banner import get_available_skills

    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "core" / "x").mkdir(parents=True)
    (a / "core" / "x" / "SKILL.md").write_text("# x\n")
    (b / "core" / "x").mkdir(parents=True)
    (b / "core" / "x" / "SKILL.md").write_text("# x dup\n")

    monkeypatch.setattr(
        "opencomputer.cli_banner._skill_search_paths", lambda: [a, b]
    )

    grouped = get_available_skills()
    assert grouped["core"] == ["x"], "duplicate skill names dedupe"


def test_get_available_tools_groups_by_plugin(monkeypatch):
    from opencomputer.cli_banner import get_available_tools

    fake_tools = {
        "Edit": "coding-harness",
        "MultiEdit": "coding-harness",
        "Read": "core",
        "Bash": "core",
    }
    monkeypatch.setattr(
        "opencomputer.cli_banner._tool_registry_snapshot", lambda: fake_tools
    )

    grouped = get_available_tools()
    assert sorted(grouped["coding-harness"]) == ["Edit", "MultiEdit"]
    assert sorted(grouped["core"]) == ["Bash", "Read"]


def test_get_available_tools_returns_empty_dict_when_registry_unreachable(
    monkeypatch,
):
    from opencomputer.cli_banner import get_available_tools

    def boom():
        raise RuntimeError("registry not initialized")
    monkeypatch.setattr(
        "opencomputer.cli_banner._tool_registry_snapshot", boom
    )

    assert get_available_tools() == {}
```

- [ ] **Step 11.2: Run, verify RED**

Run: `.venv/bin/python -m pytest tests/test_cli_banner.py -v -k "skills or tools"`
Expected: 4 FAILs.

- [ ] **Step 11.3: Implement helpers in `opencomputer/cli_banner.py`**

Replace the two stubs:

```python
def _skill_search_paths() -> list[Path]:
    """Return ordered list of dirs to walk for SKILL.md files.

    Highest-priority first (so path 0 wins on duplicate names).
    """
    import os

    paths: list[Path] = []
    home = os.environ.get("OPENCOMPUTER_HOME")
    if home:
        paths.append(Path(home) / "skills")
    else:
        paths.append(Path.home() / ".opencomputer" / "skills")

    # Bundled skills
    bundled = Path(__file__).parent / "skills"
    if bundled.exists():
        paths.append(bundled)

    return paths


def get_available_skills() -> dict[str, list[str]]:
    """Walk skill search paths; return {group: sorted-skill-names}.

    Group is the parent-of-SKILL.md directory's parent (one level up).
    Layout assumed: ``<root>/<group>/<skill>/SKILL.md``.
    """
    seen_per_group: dict[str, set[str]] = {}
    for root in _skill_search_paths():
        if not root.exists():
            continue
        for skill_md in root.rglob("SKILL.md"):
            skill_dir = skill_md.parent
            group_dir = skill_dir.parent
            if group_dir == root:
                # Skill directly under root (no group); use root name
                group = root.name
            else:
                group = group_dir.name
            seen_per_group.setdefault(group, set()).add(skill_dir.name)
    return {g: sorted(s) for g, s in sorted(seen_per_group.items())}


def _tool_registry_snapshot() -> dict[str, str]:
    """Return mapping of tool_name -> plugin_name.

    Reads from opencomputer.tools.registry's module-level `registry`
    singleton. Since BaseTool instances don't carry a plugin_id field,
    we derive the group from the tool's module path:
      - opencomputer.tools.* → "core"
      - extensions.<plugin>.* → "<plugin>"
      - other → "other"
    """
    from opencomputer.tools.registry import registry

    out: dict[str, str] = {}
    for name in registry.names():
        tool = registry.get(name)
        if tool is None:
            continue
        module = type(tool).__module__ or ""
        if module.startswith("opencomputer.tools."):
            group = "core"
        elif module.startswith("extensions."):
            parts = module.split(".")
            group = parts[1] if len(parts) > 1 else "extensions"
        else:
            group = "other"
        out[name] = group
    return out


def get_available_tools() -> dict[str, list[str]]:
    """Group registered tools by plugin-of-origin. Empty dict if registry
    isn't reachable (e.g., before plugin discovery has run)."""
    try:
        snapshot = _tool_registry_snapshot()
    except Exception:  # noqa: BLE001
        return {}
    grouped: dict[str, list[str]] = {}
    for tool_name, plugin in snapshot.items():
        grouped.setdefault(plugin, []).append(tool_name)
    return {p: sorted(names) for p, names in sorted(grouped.items())}
```

Note: `ToolRegistry.get_instance()` and `list_tools()` are assumed APIs that may need a small adapter — confirm at impl time. Fallback returning `{}` keeps banner robust if the registry surface differs slightly.

- [ ] **Step 11.4: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli_banner.py -v`
Expected: 8 PASSED.

- [ ] **Step 11.5: Commit**

```bash
git add opencomputer/cli_banner.py tests/test_cli_banner.py
git commit -m "feat(cli_banner): skill + tool discovery helpers

get_available_skills walks ~/.opencomputer/skills + bundled
opencomputer/skills, groups by parent dir, dedupes by skill name with
search-path priority order.

get_available_tools groups ToolRegistry entries by plugin-of-origin;
returns empty dict (gracefully) when registry isn't reachable so
banner-on-fresh-install doesn't crash."
```

---

## Task 12: `build_welcome_banner` (main banner assembly)

**Files:**
- Modify: `opencomputer/cli_banner.py`
- Modify: `tests/test_cli_banner.py`

- [ ] **Step 12.1: Write failing tests**

Append to `tests/test_cli_banner.py`:

```python
def test_build_welcome_banner_renders_logo_and_version(monkeypatch):
    import io
    from rich.console import Console

    from opencomputer.cli_banner import build_welcome_banner

    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_skills",
        lambda: {"coding": ["edit", "read"]},
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_tools",
        lambda: {"core": ["Edit", "Read"]},
    )

    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    build_welcome_banner(
        console, model="claude-opus-4-7", cwd="/tmp",
        session_id="abc123", home=Path("/home/user/.opencomputer"),
    )
    out = buf.getvalue()
    assert "OPENCOMPUTER" in out.upper() or "OpenComputer" in out
    assert "claude-opus-4-7" in out
    assert "abc123" in out


def test_build_welcome_banner_lists_tools_and_skills(monkeypatch):
    import io
    from rich.console import Console

    from opencomputer.cli_banner import build_welcome_banner

    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_skills",
        lambda: {"research": ["arxiv", "blogwatcher"]},
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_tools",
        lambda: {"coding-harness": ["Edit", "MultiEdit", "TodoWrite"]},
    )

    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    build_welcome_banner(console, "m", "/cwd")
    out = buf.getvalue()
    assert "research" in out
    assert "arxiv" in out
    assert "coding-harness" in out
    assert "Edit" in out


def test_build_welcome_banner_footer_counts(monkeypatch):
    import io
    from rich.console import Console

    from opencomputer.cli_banner import build_welcome_banner

    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_skills",
        lambda: {"a": ["s1", "s2"], "b": ["s3"]},  # 3 skills
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_tools",
        lambda: {"core": ["t1", "t2", "t3", "t4"]},  # 4 tools
    )

    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    build_welcome_banner(console, "m", "/cwd")
    out = buf.getvalue()
    assert "4 tools" in out
    assert "3 skills" in out
    assert "/help" in out


def test_build_welcome_banner_truncates_long_tool_lines(monkeypatch):
    import io
    from rich.console import Console

    from opencomputer.cli_banner import build_welcome_banner

    long_tool_list = [f"Tool{i:02d}" for i in range(40)]
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_skills", lambda: {}
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_tools",
        lambda: {"big": long_tool_list},
    )

    buf = io.StringIO()
    console = Console(file=buf, width=80, force_terminal=False)
    build_welcome_banner(console, "m", "/cwd")
    out = buf.getvalue()
    assert "…" in out, "Long lists must be truncated with ellipsis"
```

- [ ] **Step 12.2: Run, verify RED**

Run: `.venv/bin/python -m pytest tests/test_cli_banner.py -v -k build_welcome`
Expected: 4 FAILs (`NotImplementedError`).

- [ ] **Step 12.3: Replace `build_welcome_banner` body**

```python
import random
from rich.console import Console
from rich.text import Text

from opencomputer.cli_banner_art import OPENCOMPUTER_LOGO, SIDE_GLYPH


# Tip rotation — each tip references a real flag/env var/command.
_TIPS: tuple[str, ...] = (
    "Tip: `OPENCOMPUTER_EPHEMERAL_SYSTEM_PROMPT` injects a system prompt "
    "that's never persisted to history.",
    "Tip: Type `/help` for the slash-command list.",
    "Tip: Press Ctrl+C in chat to cancel the current turn cleanly.",
    "Tip: `oc -p <profile>` runs with a different active profile.",
    "Tip: `oc setup` re-runs the wizard — keeps your existing config "
    "by default.",
    "Tip: `/snapshot export` archives your session for later replay.",
)


def _truncate_csv(items: list[str], max_chars: int) -> str:
    """Return comma-separated items, truncated with `…` if over limit."""
    joined = ", ".join(items)
    if len(joined) <= max_chars:
        return joined
    out = []
    used = 0
    ellipsis = ", …"
    budget = max_chars - len(ellipsis)
    for it in items:
        addition = (", " if out else "") + it
        if used + len(addition) > budget:
            break
        out.append(it)
        used += len(addition)
    return ", ".join(out) + ellipsis


def build_welcome_banner(
    console: Console,
    model: str,
    cwd: str,
    *,
    session_id: Optional[str] = None,
    home: Optional[Path] = None,
) -> None:
    """Print the OPENCOMPUTER welcome banner with categorized
    tools/skills listing."""
    # 1. Logo (skip if terminal too narrow)
    width = console.size.width if console.size else 80
    longest = max(len(line) for line in OPENCOMPUTER_LOGO.splitlines() if line)
    if width >= longest:
        logo = Text(OPENCOMPUTER_LOGO, style="bold yellow")
        console.print(logo)
    else:
        console.print(Text("OPENCOMPUTER", style="bold yellow"))

    # 2. Version label (right-aligned)
    label = format_banner_version_label()
    console.print(Text(label, style="dim yellow"), justify="right")

    # 3. Side glyph + meta block
    glyph_lines = SIDE_GLYPH.strip("\n").splitlines()
    meta_lines = [
        f"[bold]{model}[/bold] · OpenComputer",
        f"[dim]{cwd}[/dim]",
    ]
    if session_id:
        meta_lines.append(f"[dim]Session: {session_id}[/dim]")
    if home:
        meta_lines.append(f"[dim]{home}[/dim]")

    # Render side-by-side using simple line-paired iteration
    width = console.size.width
    glyph_w = max(len(line) for line in glyph_lines) + 4
    meta_text = "\n".join(meta_lines)
    # Print logo glyph block first (separate row in this simple version
    # — full side-by-side render can be a polish follow-up)
    for line in glyph_lines:
        console.print(Text(line, style="bold magenta"))
    console.print()
    console.print(meta_text)

    # 4. Tools listing
    console.print()
    console.print("[bold]Available Tools[/bold]")
    tools = get_available_tools()
    line_budget = max(40, width - 12)
    for plugin in sorted(tools.keys()):
        names = tools[plugin]
        console.print(f"  [cyan]{plugin}:[/cyan] {_truncate_csv(names, line_budget)}")

    # 5. Skills listing
    console.print()
    console.print("[bold]Available Skills[/bold]")
    skills = get_available_skills()
    for group in sorted(skills.keys()):
        names = skills[group]
        console.print(f"  [magenta]{group}:[/magenta] {_truncate_csv(names, line_budget)}")

    # 6. Footer
    n_tools = sum(len(v) for v in tools.values())
    n_skills = sum(len(v) for v in skills.values())
    console.print()
    console.print(
        f"[dim]{n_tools} tools · {n_skills} skills · "
        f"[bold]/help[/bold] for commands[/dim]"
    )

    # 7. Welcome line
    console.print()
    console.print(
        "[bold]Welcome to OpenComputer![/bold] "
        "Type your message or /help for commands."
    )

    # 8. Tip
    if _TIPS:
        console.print(f"[dim]+ {random.choice(_TIPS)}[/dim]")
```

- [ ] **Step 12.4: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_cli_banner.py -v`
Expected: 12 PASSED.

- [ ] **Step 12.5: Commit**

```bash
git add opencomputer/cli_banner.py tests/test_cli_banner.py
git commit -m "feat(cli_banner): build_welcome_banner main assembly

Logo + version label + side-glyph + meta block + categorized tools/
skills listing + footer (counts + /help hint) + welcome message + tip
rotation. Long tool/skill lines truncate with ellipsis at 80-char
budget. All sources mockable for tests."
```

---

## Task 13: Wire `setup_wizard.py` re-export + `cli.py` banner hook

**Files:**
- Modify: `opencomputer/setup_wizard.py`
- Modify: `opencomputer/cli.py` (lines 905-915 region — `_run_chat_session`)
- Modify: relevant existing tests if they break

- [ ] **Step 13.1: Survey existing setup_wizard.py + cli.py preamble code**

Run: `grep -n "def run_setup\|run_setup_wizard\|^class " opencomputer/setup_wizard.py | head -10`
Run: `grep -n "OpenComputer v\|session:\|Type 'exit'" opencomputer/cli.py | head -5`

Note the current `run_setup` signature and the bare-banner block to be replaced. Adjust patches accordingly.

- [ ] **Step 13.2: Write integration test for `run_setup` re-export**

Append to `tests/test_cli_setup_wizard.py`:

```python
def test_legacy_run_setup_import_path_still_works():
    """Old callers `from opencomputer.setup_wizard import run_setup` keep working."""
    from opencomputer.setup_wizard import run_setup
    from opencomputer.cli_setup.wizard import run_setup as new_run_setup
    assert run_setup is new_run_setup
```

- [ ] **Step 13.3: Modify `opencomputer/setup_wizard.py` to re-export**

Replace existing body with:

```python
"""DEPRECATED location — preserved for backward compatibility.

The implementation moved to opencomputer.cli_setup.wizard; this module
now re-exports the public name so existing callers
(``from opencomputer.setup_wizard import run_setup``) continue to work
without source changes.
"""
from opencomputer.cli_setup.wizard import WizardCancelled, run_setup

__all__ = ["WizardCancelled", "run_setup"]
```

Note: if existing setup_wizard.py has additional public names beyond run_setup, identify them in step 13.1 and add a `# Public API kept for compat:` section that re-exports each.

- [ ] **Step 13.4: Modify `opencomputer/cli.py::_run_chat_session` to call new banner**

Locate the bare preamble (currently a `print()` of model name + session id near the start of `_run_chat_session`). Replace with:

```python
from opencomputer.cli_banner import build_welcome_banner
build_welcome_banner(
    console,  # existing rich.Console instance in cli.py
    model=cfg.model.model,
    cwd=str(Path.cwd()),
    session_id=session_id,
    home=Path(os.environ.get("OPENCOMPUTER_HOME") or Path.home() / ".opencomputer"),
)
```

If `console` doesn't exist as a local in `_run_chat_session`, instantiate one: `console = Console()`.

- [ ] **Step 13.5: Run full pytest, identify any breakage**

Run: `.venv/bin/python -m pytest tests/ --tb=short -q 2>&1 | tail -25`

Likely breaks: tests that mock `input()` for the old wizard now don't drive the new menu primitives. For each break, replace `input` mock with `radiolist` / `checklist` mock per Task 9 / 10 patterns.

- [ ] **Step 13.6: Verify legacy import test + cli.py paths green**

Run: `.venv/bin/python -m pytest tests/test_cli_setup_wizard.py::test_legacy_run_setup_import_path_still_works tests/test_cli_first_run_offer.py -v`
Expected: all PASSED.

- [ ] **Step 13.7: Commit**

```bash
git add opencomputer/setup_wizard.py opencomputer/cli.py tests/
git commit -m "feat(cli): wire new setup wizard + welcome banner into cli.py

setup_wizard.py shrinks to a re-export of cli_setup.wizard.run_setup —
existing callers unchanged.

_run_chat_session now invokes cli_banner.build_welcome_banner to print
the OPENCOMPUTER ASCII art + categorized tools/skills listing instead
of the bare model+session line.

Existing tests that mocked input() updated to mock the new menu
primitives where applicable."
```

---

## Task 14: End-to-end verification + lint + manual smoke

**Files:**
- Possibly: `tests/test_cli_setup_wizard_e2e.py` (smoke integration)

- [ ] **Step 14.1: Write end-to-end smoke integration**

Create `tests/test_cli_setup_wizard_e2e.py`:

```python
"""E2E smoke: run_setup() with all menu primitives mocked, end to end."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_e2e_first_run_picks_first_provider_and_skips_messaging(
    monkeypatch, tmp_path
):
    """Drive the full wizard: pick provider 0, skip messaging, deferred
    sections stub out. Assert config file is written with expected shape."""
    from opencomputer.cli_setup import wizard

    monkeypatch.setattr(wizard, "_resolve_config_path",
                         lambda: tmp_path / "config.yaml")

    # Sequence of radiolist returns:
    #   - inference_provider: idx 0 (first discovered provider)
    #   - messaging_platforms gate: idx 1 (skip)
    radiolist_returns = iter([0, 1])
    monkeypatch.setattr(wizard, "radiolist",
                         lambda *a, **kw: next(radiolist_returns))
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.inference_provider.radiolist",
        lambda *a, **kw: 0,
    )
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.messaging_platforms.radiolist",
        lambda *a, **kw: 1,
    )
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.inference_provider._discover_providers",
        lambda: [{"name": "anthropic", "label": "Anthropic", "description": "x"}],
    )
    monkeypatch.setattr(
        "opencomputer.cli_setup.section_handlers.inference_provider._invoke_provider_setup",
        lambda name, ctx: True,
    )

    rc = wizard.run_setup()
    assert rc == 0

    import yaml
    written = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert written["model"]["provider"] == "anthropic"
    assert "platforms" not in (written.get("gateway") or {})
```

- [ ] **Step 14.2: Run E2E + full pytest**

Run: `.venv/bin/python -m pytest tests/test_cli_setup_wizard_e2e.py -v` → expect PASS.
Run: `.venv/bin/python -m pytest tests/ --tb=short -q` → expect 6,615+ passed (baseline) +new tests passing, 4 pre-existing failures unchanged from main.

- [ ] **Step 14.3: Lint**

Run: `.venv/bin/ruff check opencomputer/cli_ui/ opencomputer/cli_setup/ opencomputer/cli_banner.py opencomputer/cli_banner_art.py opencomputer/setup_wizard.py opencomputer/cli.py tests/test_cli_*.py`
Expected: All checks passed.
If any failures: fix inline; commit fixes as `style(cli_setup): ruff lint fixes`.

- [ ] **Step 14.4: Manual smoke**

```bash
# Backup current config so we don't lose it
cp ~/.opencomputer/profiles/coding/config.yaml /tmp/config.yaml.bak

# Run setup
oc setup

# Expected:
#   1. ✦ OpenComputer Setup Wizard panel
#   2. Section "◆ Prior install detection" → deferred stub "(coming in M1)"
#   3. Section "◆ Inference Provider" → arrow-key menu, pick Anthropic
#   4. Section "◆ Messaging Platforms" → arrow-key gate, pick Skip
#   5. Sections S1-S5 → all stub-printed
#   6. ✓ Setup complete
oc chat
# Expected:
#   - OPENCOMPUTER ASCII logo
#   - Version label
#   - Side glyph + meta block (model, session)
#   - Available Tools: <plugin>: <names…>
#   - Available Skills: <group>: <names…>
#   - "<N> tools · <M> skills · /help for commands"
#   - "Welcome to OpenComputer! Type your message …"
#   - Random tip line

# Restore backup if desired
cp /tmp/config.yaml.bak ~/.opencomputer/profiles/coding/config.yaml
```

If anything looks wrong: capture diff, file follow-up notes inline; small visual tweaks land as same-PR commits.

- [ ] **Step 14.5: Final commit (any test/lint fixes from 14.2/14.3)**

```bash
git add tests/test_cli_setup_wizard_e2e.py
git commit -m "test(cli_setup): end-to-end smoke for run_setup

Drives the full wizard with mocked menu primitives + plugin discovery;
asserts config.yaml written with expected provider + no messaging
platforms (skip branch)."
```

- [ ] **Step 14.6: Push + open PR**

```bash
git push -u origin feat/hermes-onboarding-foundation
gh pr create --title "feat(onboarding): Hermes-style wizard foundation (F0+F1+F2)" --body "$(cat docs/superpowers/specs/2026-05-02-hermes-onboarding-foundation-design.md | head -50)

See full spec at docs/superpowers/specs/2026-05-02-hermes-onboarding-foundation-design.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

## Self-review

**1. Spec coverage:**

| Spec section | Covered by |
|---|---|
| F0 — radiolist | Tasks 2, 3 |
| F0 — checklist | Task 4 |
| F0 — single_select | Task 5 |
| F0 — numbered fallback | Tasks 2, 4, 5 |
| F0 — flush_stdin | Task 2 |
| F0 — style module | Task 1 |
| F1 — WizardSection / WizardCtx / SectionResult | Task 6 |
| F1 — orchestrator (run_setup) | Task 7 |
| F1 — _deferred handler factory | Task 6 |
| F1 — inference_provider section | Task 8 |
| F1 — messaging_platforms section | Task 9 |
| F1 — `configured_check` keep/reconfigure/skip gate | Task 7 |
| F1 — config persistence | Task 7 |
| F2 — ASCII art constants | Task 10 |
| F2 — version label helper | Task 10 |
| F2 — get_available_skills | Task 11 |
| F2 — get_available_tools | Task 11 |
| F2 — build_welcome_banner | Task 12 |
| F2 — tip rotation | Task 12 |
| Wiring — setup_wizard.py re-export | Task 13 |
| Wiring — cli.py banner hook | Task 13 |
| E2E + lint + manual smoke | Task 14 |

All spec sections covered. ✓

**2. Placeholder scan:**
- Any "TBD", "TODO", "implement later", "fill in"? Searched: no occurrences in plan body except deferred-handler stub messages (`(coming in M1)`) which are intentional UX strings.
- "Add appropriate error handling" / "add validation" / "handle edge cases" without specifics? None.
- Vague code blocks? Reviewed; each block contains complete code or explicit "Note:" rationale for skeleton level.

**3. Type consistency check:**
- `Choice` dataclass fields (`label`, `value`, `description`) referenced consistently across Tasks 2, 3, 4, 5, 8, 9, 12. ✓
- `SectionResult` enum values used consistently (CONFIGURED / SKIPPED_KEEP / SKIPPED_FRESH / CANCELLED). ✓
- `WizardCtx` fields (config, config_path, is_first_run, quick_mode, extra) consistent. ✓
- `WizardSection` fields (key, icon, title, description, handler, configured_check, deferred, target_subproject) consistent. ✓
- `WizardCancelled` defined in `cli_ui.menu`, re-exported from `cli_setup.wizard` — both import paths used in tests; consistent. ✓

**4. Self-audit findings (resolved inline):**

| Finding | Resolution |
|---|---|
| `ToolRegistry.get_instance()` doesn't exist; singleton is module-level `registry` | Task 11 updated to use `from opencomputer.tools.registry import registry` and group by tool's `__module__` path. |
| `_resolve_config_path` reinvents what `opencomputer.agent.config_store.config_file_path` already does | Task 7 wrapper now delegates to `config_file_path()`. |
| `configured_check` raising mid-wizard would crash the whole wizard | Task 7 adds `_safe_configured_check` helper that swallows handler exceptions and treats as "not configured". |
| Ctrl+C top-level would dump traceback instead of clean exit | Task 7's `run_setup` wraps the section loop in `try: ... except KeyboardInterrupt:` returning rc=1. |
| Banner logo overflows narrow terminals (e.g., 60-col CI logs) | Task 12 width-checks before rendering; falls back to plain "OPENCOMPUTER" text when too narrow. |
| Existing `setup_wizard.run_setup() -> None`; new returns `int` | Verified both call sites in cli.py ignore return value (line ~819 + the `setup` Typer command). Safe to widen return type. |
| Plugin `SetupProvider`/`SetupChannel` field names | Verified in `plugin_sdk/core.py:128/188` — `name`, `label`, `description`, `env_vars` all confirmed. |

Remaining lower-priority items deferred to impl-time judgment:
- Step 13.1 still surveys setup_wizard.py for any additional public names (the existing module has many `_*` privates but only `run_setup` is public — confirmed in audit).
- Banner could integrate `prefetch_update_check`'s result line ("update available: vX.Y.Z"); can land as same-PR follow-up commit if visually desired.
- Skill discovery rglob is O(N) in skill count; cached lookup is unnecessary for current ~150 skills but worth a `@functools.lru_cache` decorator if scaling concerns surface.

---

## Execution choice

Plan complete and saved to `docs/superpowers/plans/2026-05-02-hermes-onboarding-foundation.md`.

Recommended execution path: **Subagent-driven**, since the user requested self-audit before execution and subagent-driven dispatches a code-reviewer between tasks naturally. Per memory `feedback_subagent_models.md`: default opus + extended thinking; never haiku.
