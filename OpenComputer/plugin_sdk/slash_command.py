"""Plugin-authored slash commands.

A plugin can expose in-chat slash commands (e.g. ``/plan``, ``/diff``)
by registering a SlashCommand subclass via
``PluginAPI.register_slash_command``. The host dispatches any message
whose first token starts with ``/`` to the matching command.

Formalization of the Phase-6f duck-typed contract. Legacy duck-typed
commands (any object with ``name``, ``description``, ``execute``) are
still accepted by the dispatcher for backwards compat.

``execute`` is **async** because it can touch filesystem state
(checkpoints, rewind), call back into the agent, and integrates with
the already-async agent loop. Legacy synchronous-returning commands
that return a plain string are also accepted — the dispatcher wraps
their return value in a ``SlashCommandResult`` for callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from plugin_sdk.runtime_context import RuntimeContext


@dataclass(frozen=True, slots=True)
class SlashCommandResult:
    """What a slash command returns when executed."""

    #: Text to show the user.
    output: str
    #: True if the command is "terminal" — i.e. it handled the user's intent
    #: and the agent loop should NOT continue to the LLM for this turn.
    #: False means the agent loop proceeds as normal (command was a side-
    #: effect like ``/plan`` that sets a flag and lets chat continue).
    handled: bool = True
    #: Origin of this result.
    #:
    #: ``"command"`` (default) — handler was a registered command (built-in
    #: or plugin-authored). The agent loop emits the output as a normal
    #: assistant text reply.
    #:
    #: ``"skill"`` — handler was the slash-skill fallback that loaded a
    #: SKILL.md body. The agent loop wraps the result as a synthetic
    #: ``Skill`` ``tool_use`` + ``tool_result`` pair so the model sees the
    #: skill content as authoritative tool output (Claude-Code parity).
    #:
    #: Default ``"command"`` keeps existing call sites working unchanged.
    source: Literal["command", "skill"] = "command"


class SlashCommand(ABC):
    """Base class for plugin-authored slash commands."""

    #: The leading-slash name the user types. E.g. ``"plan"`` for ``/plan``.
    #: No leading slash. Alphanumeric + hyphen.
    name: str = ""

    #: One-line description shown in ``/help`` listings.
    description: str = ""

    @abstractmethod
    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        """Run the command. ``args`` is everything after ``/<name>``.

        Must not raise. On failure return a SlashCommandResult with
        output describing the error + handled=True.
        """


__all__ = ["SlashCommand", "SlashCommandResult"]
