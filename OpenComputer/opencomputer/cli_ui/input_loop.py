"""Input layer for the chat loop.

Replaces ``Console.input(...)`` with a ``prompt_toolkit.PromptSession``
that supports:

- Persistent ``FileHistory`` (Up-arrow recalls across sessions)
- ``Alt+Enter`` / ``Ctrl+J`` insert literal newline (multi-line input)
- ``Esc`` (during the prompt phase) clears the input buffer
- Bracketed paste (handled automatically by prompt_toolkit)
- ``mouse_support=False`` (we want native terminal selection for copy)

Mid-stream ESC interrupt is NOT handled here — see
:mod:`opencomputer.cli_ui.keyboard_listener` (a daemon thread that runs
during streaming when prompt_toolkit isn't active).
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
    """Strip trailing whitespace. Multi-line input keeps inner formatting."""
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
        # ESC during *streaming*: handled by KeyboardListener thread; the
        # prompt isn't the active app at that point.
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
        multiline=False,
        mouse_support=False,
        enable_history_search=True,
        complete_while_typing=False,
        # erase_when_done clears the typed prompt line on submit so the
        # chat loop can re-render the user's message inside a styled
        # boundary box (no duplicate "you › ..." line in scrollback).
        erase_when_done=True,
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
