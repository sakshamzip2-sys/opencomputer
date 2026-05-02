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
(re-exported from cli_setup.wizard) to propagate cancellation cleanly
through nested handlers.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Optional


class WizardCancelled(Exception):
    """ESC pressed in a menu primitive — propagates cleanly through
    nested section handlers without each having to check return values.

    Re-exported from opencomputer.cli_setup.wizard for public callers.
    Lives here in menu.py so menu primitives can raise it without
    importing the wizard module (avoid circular import).
    """


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
    *,
    _input: Optional[Any] = None,   # injection for tests
    _output: Optional[Any] = None,
) -> int:
    """Single-select menu. Returns selected index. Raises WizardCancelled
    on ESC.

    On TTY: arrow-key navigation via prompt_toolkit Application.
    On non-TTY: numbered prompt via stdin.
    """
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
