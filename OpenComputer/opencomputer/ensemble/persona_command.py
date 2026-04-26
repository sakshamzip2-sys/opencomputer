"""``/persona`` slash command (Phase 7.A of catch-up plan).

Usage::

    /persona              → list available personas + show active
    /persona <name>       → switch to <name> for the rest of the session

The command is intentionally minimal — it only mutates the
:class:`PersonaSwitcher`'s ``current`` field. Everything downstream
(agent-loop prompt refresh, [persona: foo] turn prefix, session-DB
labelling) is the caller's responsibility, wired via the
``on_switch`` callback at construction time.
"""

from __future__ import annotations

from opencomputer.ensemble.switcher import PersonaNotFound, PersonaSwitcher
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class PersonaSlashCommand(SlashCommand):
    """``/persona [<name>]`` — list or switch persona within a session.

    Construct with a :class:`PersonaSwitcher` instance:

        api.register_slash_command(PersonaSlashCommand(switcher))
    """

    name: str = "persona"
    description: str = "List or switch persona for the current session"

    def __init__(self, switcher: PersonaSwitcher) -> None:
        self._switcher = switcher

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        target = args.strip()
        if not target:
            # Listing mode
            available = self._switcher.known_profiles()
            if not available:
                return SlashCommandResult(
                    output="(no personas configured — populate "
                    f"{self._switcher.profiles_root} with subdirs)",
                )
            lines = [f"Active persona: {self._switcher.current}", "Available:"]
            for n in available:
                marker = " (active)" if n == self._switcher.current else ""
                lines.append(f"  - {n}{marker}")
            return SlashCommandResult(output="\n".join(lines))

        try:
            self._switcher.switch_to(target)
        except PersonaNotFound as e:
            return SlashCommandResult(output=f"Error: {e}")

        return SlashCommandResult(
            output=f"[persona switched: {target}]"
        )
