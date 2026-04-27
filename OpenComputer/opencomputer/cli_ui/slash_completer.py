"""Slash command autocomplete for the OpenComputer TUI.

Provides a :class:`prompt_toolkit.completion.Completer` that yields
completions for slash commands when the user is typing a slash-command
name (line starts with ``/`` and no space has been entered yet). Reads
:data:`opencomputer.cli_ui.slash.SLASH_REGISTRY` directly — single source
of truth, so adding or removing a command in ``slash.py`` is reflected
automatically in the dropdown.

Aliases dispatch as before via :func:`slash_handlers.dispatch_slash`,
but they are NOT shown as separate rows in the dropdown — one row per
canonical command, mirroring Claude Code's convention.
"""
from __future__ import annotations

from typing import Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from .slash import SLASH_REGISTRY, CommandDef


def longest_common_prefix(strs: list[str]) -> str:
    """Return the longest common prefix of all strings in ``strs``.

    Empty list returns the empty string. Comparison is case-sensitive;
    callers that need case-insensitive matching should normalize first.
    """
    if not strs:
        return ""
    s_min = min(strs)
    s_max = max(strs)
    for i, ch in enumerate(s_min):
        if ch != s_max[i]:
            return s_min[:i]
    return s_min


def _format_display(cmd: CommandDef) -> str:
    if cmd.args_hint:
        return f"/{cmd.name} {cmd.args_hint}"
    return f"/{cmd.name}"


class SlashCommandCompleter(Completer):
    """Yields :class:`Completion` rows for slash commands.

    Activates only when the buffer starts with ``/`` and the cursor is
    still inside the command-name token (no space yet). Returns nothing
    for plain chat input, so prompt_toolkit's default behavior — no
    dropdown — applies for normal messages.
    """

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " in text:
            return
        prefix = text[1:].lower()
        matches = [cmd for cmd in SLASH_REGISTRY if cmd.name.startswith(prefix)]
        matches.sort(key=lambda c: c.name)
        for cmd in matches:
            yield Completion(
                text=f"/{cmd.name}",
                start_position=-len(text),
                display=_format_display(cmd),
                display_meta=cmd.description,
            )
