"""Arrow-key menu primitives for setup wizards and terminal workflows."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

_IS_WINDOWS = os.name == "nt"


class WizardCancelled(Exception):  # noqa: N818
    """Raised when a user cancels an interactive menu."""


@dataclass(frozen=True)
class Choice:
    """One menu entry."""

    label: str
    value: object
    description: str | None = None


def flush_stdin() -> None:
    """Best-effort drain of stale keypresses before a prompt_toolkit menu."""
    try:
        if not sys.stdin.isatty():
            return
        import termios

        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except (termios.error, OSError):
            pass
    except ImportError:
        pass


def _install_number_bindings(
    bindings: Any,
    *,
    cursor: list[int],
    count: int,
    clear_on_confirm: bool = False,
) -> list[str]:
    """Bind 0-9 and backspace to a shared 1-based numeric cursor jump.

    Digits build a short buffer, so typing ``38`` jumps to row 38 before
    Enter/Space confirms. Arrow navigation should clear this buffer.
    """
    number_buffer: list[str] = []

    def apply_number_buffer() -> None:
        if not number_buffer:
            return
        try:
            n = int("".join(number_buffer))
        except ValueError:
            return
        if 1 <= n <= count:
            cursor[0] = n - 1

    for digit in "0123456789":
        @bindings.add(digit)
        def _digit(event, digit=digit):
            number_buffer.append(digit)
            apply_number_buffer()

    @bindings.add("backspace")
    @bindings.add("c-h")
    def _backspace(event):
        if number_buffer:
            number_buffer.pop()
            apply_number_buffer()

    if clear_on_confirm:
        @bindings.add("c-m")
        def _enter(event):
            number_buffer.clear()

    return number_buffer


def _clear_number_buffer(buf: list[str]) -> None:
    buf.clear()


def _menu_window(render: Any):
    """Build a menu window that never paints cursor-line reverse video."""
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    return Window(
        FormattedTextControl(render, focusable=False, show_cursor=False),
        always_hide_cursor=True,
        dont_extend_width=True,
    )


def _menu_application(
    render: Any,
    bindings: Any,
    *,
    style: Any,
    _input: Any | None = None,
    _output: Any | None = None,
):
    """Build a selector application rendered inside the wizard screen."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit

    return Application(
        layout=Layout(HSplit([_menu_window(render)])),
        key_bindings=bindings,
        style=style,
        full_screen=False,
        erase_when_done=False,
        input=_input,
        output=_output,
    )


def _should_use_prompt_toolkit(_input: Any | None = None) -> bool:
    """Return True when an interactive prompt_toolkit menu can be used.

    Some Windows terminal launch paths report ``stdin`` as non-TTY while
    ``stdout`` is still an interactive console. In that case numbered
    fallback breaks arrow-key selection for commands like ``oc model``.
    """
    if _input is not None:
        return True
    if sys.stdin.isatty():
        return True
    return _IS_WINDOWS and sys.stdout.isatty()


def radiolist(
    question: str,
    choices: list[Choice],
    default: int = 0,
    description: str | None = None,
    *,
    _input: Any | None = None,
    _output: Any | None = None,
) -> int:
    """Single-select menu. Returns selected index."""
    if not _should_use_prompt_toolkit(_input):
        return _radiolist_numbered_fallback(question, choices, default, description)

    flush_stdin()

    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings

    from opencomputer.cli_ui.style import (
        ARROW_GLYPH,
        MENU_STYLE,
        RADIO_OFF,
        RADIO_ON,
    )

    cursor = [default]
    bindings = KeyBindings()
    number_buffer = _install_number_bindings(
        bindings, cursor=cursor, count=len(choices)
    )

    def render():
        hint = "↑↓ navigate  numbers jump  ENTER/SPACE select  ESC cancel"
        if number_buffer:
            hint += f"  Choice: {''.join(number_buffer)}"
        lines: list[tuple[str, str]] = [
            ("class:menu.title", question + "\n"),
            ("class:menu.hint", hint + "\n"),
        ]
        if description:
            lines.append(("class:menu.description", f"  {description}\n"))
        lines.append(("", "\n"))
        for i, c in enumerate(choices):
            is_sel = i == cursor[0]
            arrow_class = "class:menu.selected.arrow" if is_sel else "class:"
            row_class = "class:menu.selected" if is_sel else "class:"
            glyph_class = (
                "class:menu.selected.glyph"
                if is_sel
                else "class:menu.unselected.glyph"
            )
            arrow = ARROW_GLYPH if is_sel else " "
            glyph = RADIO_ON if is_sel else RADIO_OFF
            suffix = f"  ({c.description})" if c.description else ""
            lines.append((arrow_class, f" {arrow} "))
            lines.append((glyph_class, f"({glyph}) "))
            lines.append((row_class, f"{c.label}{suffix}\n"))
        return FormattedText(lines)

    @bindings.add("up")
    def _up(event):
        _clear_number_buffer(number_buffer)
        cursor[0] = (cursor[0] - 1) % len(choices)

    @bindings.add("down")
    def _down(event):
        _clear_number_buffer(number_buffer)
        cursor[0] = (cursor[0] + 1) % len(choices)

    @bindings.add("enter")
    @bindings.add(" ")
    def _select(event):
        event.app.exit(result=cursor[0])

    @bindings.add("escape")
    @bindings.add("c-c")
    def _cancel(event):
        event.app.exit(exception=WizardCancelled())

    app = _menu_application(
        render,
        bindings,
        style=MENU_STYLE,
        _input=_input,
        _output=_output,
    )
    return app.run()


def _radiolist_numbered_fallback(
    question: str,
    choices: list[Choice],
    default: int = 0,
    description: str | None = None,
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
        except (EOFError, OSError):
            return default
        if raw == "":
            return default
        try:
            n = int(raw)
        except ValueError:
            print(f"Invalid input '{raw}' - enter a number.", file=sys.stderr)
            continue
        if not (1 <= n <= len(choices)):
            print(f"out of range - enter 1-{len(choices)}.", file=sys.stderr)
            continue
        return n - 1


def checklist(
    title: str,
    items: list[Choice],
    pre_selected: list[int] | None = None,
    *,
    show_markers: bool = True,
    _input: Any | None = None,
    _output: Any | None = None,
) -> list[int]:
    """Multi-select menu. Returns sorted list of selected indices."""
    pre_selected = pre_selected or []
    if not _should_use_prompt_toolkit(_input):
        return _checklist_numbered_fallback(title, items, pre_selected)

    flush_stdin()

    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings

    from opencomputer.cli_ui.style import ARROW_GLYPH, CHECK_OFF, CHECK_ON, MENU_STYLE

    cursor = [0]
    selected: set[int] = set(pre_selected)
    bindings = KeyBindings()
    number_buffer = _install_number_bindings(bindings, cursor=cursor, count=len(items))

    def render():
        hint = "↑↓ navigate  numbers jump  SPACE toggle  ENTER confirm  ESC cancel"
        if number_buffer:
            hint += f"  Choice: {''.join(number_buffer)}"
        lines: list[tuple[str, str]] = [
            ("class:menu.title", title + "\n"),
            ("class:menu.hint", hint + "\n"),
            ("", "\n"),
        ]
        for i, c in enumerate(items):
            is_cur = i == cursor[0]
            is_sel = i in selected
            arrow = ARROW_GLYPH if is_cur else " "
            glyph = CHECK_ON if is_sel else CHECK_OFF
            arrow_class = "class:menu.selected.arrow" if is_cur else "class:"
            row_class = "class:menu.selected" if is_cur else "class:"
            glyph_class = (
                "class:menu.selected.glyph"
                if is_sel
                else "class:menu.unselected.glyph"
            )
            suffix = f"  ({c.description})" if c.description else ""
            lines.append((arrow_class, f" {arrow} "))
            if show_markers:
                lines.append((glyph_class, f"[{glyph}] "))
            lines.append((row_class, f"{c.label}{suffix}\n"))
        return FormattedText(lines)

    @bindings.add("up")
    def _up(event):
        _clear_number_buffer(number_buffer)
        cursor[0] = (cursor[0] - 1) % len(items)

    @bindings.add("down")
    def _down(event):
        _clear_number_buffer(number_buffer)
        cursor[0] = (cursor[0] + 1) % len(items)

    @bindings.add(" ")
    def _toggle(event):
        _clear_number_buffer(number_buffer)
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

    app = _menu_application(
        render,
        bindings,
        style=MENU_STYLE,
        _input=_input,
        _output=_output,
    )
    return app.run()


def _checklist_numbered_fallback(
    title: str,
    items: list[Choice],
    pre_selected: list[int],
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
            raw = input(f"Numbers comma-separated [default {pre_str}]: ").strip()
        except (EOFError, OSError):
            return sorted(pre_selected)
        if raw == "":
            return sorted(pre_selected)
        try:
            picks = sorted({int(x.strip()) - 1 for x in raw.split(",") if x.strip()})
        except ValueError:
            print(f"Invalid input '{raw}' - comma-separated numbers only.", file=sys.stderr)
            continue
        if not all(0 <= p < len(items) for p in picks):
            print(f"out of range - only 1-{len(items)} are valid.", file=sys.stderr)
            continue
        return picks


def single_select(
    title: str,
    items: list[Choice],
    default: int = 0,
    *,
    _input: Any | None = None,
    _output: Any | None = None,
) -> int:
    """Single-select menu without radio glyphs."""
    if not _should_use_prompt_toolkit(_input):
        return _single_select_numbered_fallback(title, items, default)

    flush_stdin()

    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings

    from opencomputer.cli_ui.style import ARROW_GLYPH, MENU_STYLE

    cursor = [default]
    bindings = KeyBindings()
    number_buffer = _install_number_bindings(bindings, cursor=cursor, count=len(items))

    def render():
        hint = "↑↓ navigate  numbers jump  ENTER select  ESC cancel"
        if number_buffer:
            hint += f"  Choice: {''.join(number_buffer)}"
        lines: list[tuple[str, str]] = [
            ("class:menu.title", title + "\n"),
            ("class:menu.hint", hint + "\n"),
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

    @bindings.add("up")
    def _up(event):
        _clear_number_buffer(number_buffer)
        cursor[0] = (cursor[0] - 1) % len(items)

    @bindings.add("down")
    def _down(event):
        _clear_number_buffer(number_buffer)
        cursor[0] = (cursor[0] + 1) % len(items)

    @bindings.add("enter")
    def _select(event):
        event.app.exit(result=cursor[0])

    @bindings.add("escape")
    @bindings.add("c-c")
    def _cancel(event):
        event.app.exit(exception=WizardCancelled())

    app = _menu_application(
        render,
        bindings,
        style=MENU_STYLE,
        _input=_input,
        _output=_output,
    )
    return app.run()


def _single_select_numbered_fallback(
    title: str,
    items: list[Choice],
    default: int,
) -> int:
    """Non-TTY single-select without radio glyphs."""
    print(title)
    for i, c in enumerate(items):
        marker = "→" if i == default else " "
        suffix = f"  ({c.description})" if c.description else ""
        print(f"  {marker} {i + 1}. {c.label}{suffix}")
    print()
    while True:
        try:
            raw = input(f"Choice [1-{len(items)}, default {default + 1}]: ").strip()
        except (EOFError, OSError):
            return default
        if raw == "":
            return default
        try:
            n = int(raw)
        except ValueError:
            print(f"Invalid input '{raw}'.", file=sys.stderr)
            continue
        if not 1 <= n <= len(items):
            print("out of range.", file=sys.stderr)
            continue
        return n - 1
