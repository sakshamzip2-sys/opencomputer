"""Slash command registry + dispatcher.

Pattern adapted from hermes-agent's ``CommandDef`` registry. The registry
is a flat ``list[CommandDef]`` — single source of truth — and lookups are
built lazily as needed. Handlers live in :mod:`slash_handlers`; this
module owns only metadata + resolution so tests can exercise the registry
without importing Rich/prompt_toolkit.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CommandDef:
    """One slash command. Handlers are looked up by name in
    :mod:`slash_handlers` rather than stored here so the registry stays
    importable in test contexts that don't have Console."""

    name: str
    description: str
    category: str = "general"
    aliases: tuple[str, ...] = field(default_factory=tuple)
    args_hint: str = ""


@dataclass
class SlashResult:
    """What happened when a slash command ran.

    - ``handled``: True if the input was recognized as a slash command
      (regardless of success). False means the chat loop should treat
      the input as a normal user message instead.
    - ``exit_loop``: True if the loop should terminate after this command
      (e.g. ``/exit``).
    - ``message``: optional human-readable status to print.
    """

    handled: bool
    exit_loop: bool = False
    message: str = ""


SLASH_REGISTRY: list[CommandDef] = [
    CommandDef(
        name="exit",
        description="Exit the chat session.",
        category="session",
        aliases=("quit", "q"),
    ),
    CommandDef(
        name="clear",
        description="Clear the screen and start a fresh session id.",
        category="session",
        aliases=("new", "reset"),
    ),
    CommandDef(
        name="help",
        description="Show available slash commands.",
        category="meta",
        aliases=("h", "?"),
    ),
    CommandDef(
        name="screenshot",
        description="Save a snapshot of the current rendered output.",
        category="output",
        aliases=("snap",),
        args_hint="[path]",
    ),
    CommandDef(
        name="export",
        description="Export the full transcript to a file (markdown).",
        category="output",
        args_hint="[path]",
    ),
    CommandDef(
        name="cost",
        description="Show cumulative input/output tokens for this session.",
        category="meta",
    ),
    CommandDef(
        name="model",
        description="Show or switch the active model.",
        category="config",
        args_hint="[provider/model]",
    ),
    CommandDef(
        name="sessions",
        description="List recent sessions.",
        category="session",
        aliases=("history",),
    ),
    CommandDef(
        name="rename",
        description="Set a friendly title for the current session.",
        category="session",
        aliases=("title",),
        args_hint="<new title>",
    ),
    CommandDef(
        name="resume",
        description="Switch to a prior session (interactive picker by default).",
        category="session",
        args_hint="[last|<session-id-prefix>]",
    ),
    CommandDef(
        name="snapshot",
        description="Archive critical state files (session db + config + .env + ...).",
        category="session",
        args_hint="[create [<label>]|list|restore <id>|prune]",
    ),
]


def _build_lookup() -> dict[str, CommandDef]:
    out: dict[str, CommandDef] = {}
    for cmd in SLASH_REGISTRY:
        out[cmd.name] = cmd
        for alias in cmd.aliases:
            out[alias] = cmd
    return out


_LOOKUP: dict[str, CommandDef] = _build_lookup()


def is_slash_command(text: str) -> bool:
    """True iff text starts with ``/`` followed by at least one non-space char."""
    if not text or not text.startswith("/"):
        return False
    rest = text[1:].lstrip()
    return bool(rest)


def resolve_command(name: str) -> CommandDef | None:
    """Resolve a name (with or without leading ``/``) to a CommandDef."""
    n = name.lstrip("/").strip().lower()
    return _LOOKUP.get(n)
