"""Arrow-key picker — TerminalMenu primary, curses fallback, numbered fallback.

Hermes-parity UX (2026-04-30). Hermes uses ``simple_term_menu.TerminalMenu``
for its model picker, with a curses fallback for the provider step and a
numbered-list fallback when neither library is available. This module
ports that three-tier fallback chain so OpenComputer's ``oc model`` and
other interactive pickers feel identical to Hermes' arrow-key UX.

Public API:
    pick_one(title, choices, current_idx=0, allow_cancel=True) -> int | None

Returns the selected index (0-based), or None on cancel.
"""
from __future__ import annotations

import sys
from collections.abc import Sequence


def _flush_stdin() -> None:
    """Drain any stray bytes the terminal-mode library left behind.

    Must run AFTER curses.wrapper() / TerminalMenu / any tty-mode lib
    returns and BEFORE the next input() / getpass.getpass() — otherwise
    leftover escape-sequence bytes from arrow keys get consumed by the
    next read, corrupting user input. Hermes hit this exact bug; ported
    verbatim from ``hermes_cli/curses_ui.py:flush_stdin``.
    """
    try:
        if not sys.stdin.isatty():
            return
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:  # noqa: BLE001
        pass


class _TierUnavailableError(Exception):
    """Raised when a picker tier can't run (missing lib, no TTY).

    Distinguished from the tier returning ``None`` (meaning the user
    deliberately cancelled). The caller falls through to the next tier
    on this exception only.
    """


def _try_terminal_menu(
    title: str,
    choices: Sequence[str],
    current_idx: int,
    allow_cancel: bool,
) -> int | None:
    """First-choice picker — simple_term_menu (arrow keys + cycle).

    Raises ``_TierUnavailableError`` if the library isn't installed or
    the terminal can't host an interactive menu. Returns int on pick,
    None on user cancel (ESC / q).
    """
    try:
        from simple_term_menu import TerminalMenu
    except ImportError as e:
        raise _TierUnavailableError("simple_term_menu not installed") from e
    if not sys.stdin.isatty():
        raise _TierUnavailableError("not a TTY")
    try:
        menu = TerminalMenu(
            menu_entries=list(choices),
            title=title,
            cursor_index=current_idx,
            menu_cursor="-> ",
            menu_cursor_style=("fg_yellow", "bold"),
            menu_highlight_style=("bg_black", "fg_green"),
            cycle_cursor=True,
            clear_screen=False,
            show_search_hint=True,
            quit_keys=("escape", "q") if allow_cancel else (),
        )
        result = menu.show()
    except (NotImplementedError, OSError) as e:
        raise _TierUnavailableError(
            f"TerminalMenu raised {type(e).__name__}",
        ) from e
    if result is None:
        return None
    if isinstance(result, tuple):
        result = result[0]
    return int(result)


def _try_curses(
    title: str,
    choices: Sequence[str],
    current_idx: int,
    allow_cancel: bool,
) -> int | None:
    """Second-choice picker — curses radiolist (arrow keys, no scroll-search).

    Raises ``_TierUnavailableError`` if curses can't run. Returns int on
    pick, None on user cancel.
    """
    if not sys.stdin.isatty():
        raise _TierUnavailableError("not a TTY")
    try:
        import curses
    except ImportError as e:
        raise _TierUnavailableError("curses not installed") from e

    items = list(choices)
    cancel_value: int | None = None if allow_cancel else current_idx
    result_holder: list[int | None] = [cancel_value]

    def _draw(stdscr) -> None:
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
        cursor = current_idx
        scroll = 0

        while True:
            stdscr.clear()
            max_y, max_x = stdscr.getmaxyx()
            row = 0
            try:
                hattr = curses.A_BOLD
                if curses.has_colors():
                    hattr |= curses.color_pair(2)
                stdscr.addnstr(row, 0, title, max_x - 1, hattr)
                row += 1
                stdscr.addnstr(
                    row, 0,
                    "  ↑↓ navigate  ENTER select  ESC cancel",
                    max_x - 1,
                    curses.A_DIM,
                )
                row += 1
            except curses.error:
                pass

            items_start = row + 1
            visible = max_y - items_start - 1
            if cursor < scroll:
                scroll = cursor
            elif cursor >= scroll + visible:
                scroll = cursor - visible + 1

            for di, i in enumerate(
                range(scroll, min(len(items), scroll + visible)),
            ):
                y = di + items_start
                if y >= max_y - 1:
                    break
                radio = "●" if i == current_idx else "○"
                arrow = "→" if i == cursor else " "
                line = f" {arrow} ({radio}) {items[i]}"
                attr = curses.A_NORMAL
                if i == cursor:
                    attr = curses.A_BOLD
                    if curses.has_colors():
                        attr |= curses.color_pair(1)
                try:
                    stdscr.addnstr(y, 0, line, max_x - 1, attr)
                except curses.error:
                    pass

            stdscr.refresh()
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                cursor = (cursor - 1) % len(items)
            elif key in (curses.KEY_DOWN, ord("j")):
                cursor = (cursor + 1) % len(items)
            elif key in (ord(" "), curses.KEY_ENTER, 10, 13):
                result_holder[0] = cursor
                return
            elif key in (27, ord("q")):
                result_holder[0] = cancel_value
                return

    try:
        curses.wrapper(_draw)
    except Exception as e:  # noqa: BLE001
        raise _TierUnavailableError(
            f"curses raised {type(e).__name__}",
        ) from e
    finally:
        _flush_stdin()
    return result_holder[0]


def _numbered_fallback(
    title: str,
    choices: Sequence[str],
    current_idx: int,
    allow_cancel: bool,
) -> int | None:
    """Last-resort picker — numbered list + input(). Used when no TTY,
    no simple_term_menu, no curses (CI, redirected stdin, etc.)."""
    print(f"\n  {title}")
    for i, label in enumerate(choices):
        marker = "*" if i == current_idx else " "
        print(f"  {marker} [{i + 1}] {label}")
    print()
    suffix = " (Enter to cancel)" if allow_cancel else ""
    while True:
        try:
            raw = input(f"  Pick a number{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            if allow_cancel:
                return None
            continue
        if not raw.isdigit():
            print("  (._.) Please enter a number.")
            continue
        idx = int(raw) - 1
        if 0 <= idx < len(choices):
            return idx
        print(f"  (._.) Out of range. Pick 1..{len(choices)}.")


def pick_one(
    title: str,
    choices: Sequence[str],
    *,
    current_idx: int = 0,
    allow_cancel: bool = True,
) -> int | None:
    """Three-tier fallback picker: TerminalMenu → curses → numbered.

    Args:
        title: Header line shown above the list.
        choices: Display labels.
        current_idx: 0-based index that starts highlighted (Hermes
            convention — the "currently in use" entry pre-cursors here).
        allow_cancel: True → ESC/q/Enter on empty returns None.

    Returns:
        Selected index (0-based), or None if cancelled.
    """
    if not choices:
        return None
    current_idx = max(0, min(current_idx, len(choices) - 1))

    for tier in (_try_terminal_menu, _try_curses):
        try:
            return tier(title, choices, current_idx, allow_cancel)
        except _TierUnavailableError:
            continue

    return _numbered_fallback(title, choices, current_idx, allow_cancel)


__all__ = ["pick_one", "_flush_stdin"]
