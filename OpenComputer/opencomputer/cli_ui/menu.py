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
from typing import Optional


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
) -> int:
    """Single-select menu. Returns selected index.

    On TTY: arrow-key navigation via prompt_toolkit (Task 3 lands this).
    On non-TTY: numbered prompt via stdin.
    """
    if not sys.stdin.isatty():
        return _radiolist_numbered_fallback(question, choices, default, description)

    # TTY path is implemented in Task 3. For now (this commit only),
    # fall through to numbered fallback so this task's tests pass even
    # on a TTY-test-environment.
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
