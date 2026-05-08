"""``/details [section] [mode]`` — TUI section visibility setter.

Hermes-CLI parity (doc lines 281-284). Two argument shapes:

- ``/details [hidden|collapsed|expanded|cycle]`` — global default.
- ``/details <section> [hidden|collapsed|expanded|reset]`` — per-section
  override.

Sections: ``thinking``, ``tools``, ``subagents``, ``activity``.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_MODES = ("hidden", "collapsed", "expanded")
_SECTIONS = ("thinking", "tools", "subagents", "activity")
_CYCLE = ("collapsed", "expanded", "hidden")


def _next_in_cycle(current: str) -> str:
    try:
        idx = _CYCLE.index(current)
    except ValueError:
        idx = -1
    return _CYCLE[(idx + 1) % len(_CYCLE)]


class DetailsCommand(SlashCommand):
    name = "details"
    description = "TUI section visibility (global or per-section)."

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        parts = (args or "").split()
        sections = runtime.custom.setdefault("sections", {})

        if not parts:
            mode = runtime.custom.get("details_mode", "collapsed")
            return SlashCommandResult(
                output=(
                    f"details mode: {mode}\nsections: {dict(sections)}"
                ),
                handled=True,
            )

        first = parts[0].lower()

        # Global form: /details <mode>
        if first in _MODES or first == "cycle":
            current = runtime.custom.get("details_mode", "collapsed")
            new = _next_in_cycle(current) if first == "cycle" else first
            runtime.custom["details_mode"] = new
            return SlashCommandResult(
                output=f"details mode: {new}", handled=True
            )

        # Per-section form: /details <section> <mode|reset>
        if first in _SECTIONS:
            if len(parts) < 2:
                return SlashCommandResult(
                    output=(
                        f"Usage: /details {first} "
                        f"[{ '|'.join(_MODES) }|reset]"
                    ),
                    handled=True,
                )
            mode = parts[1].lower()
            if mode == "reset":
                sections.pop(first, None)
                return SlashCommandResult(
                    output=(
                        f"section {first}: reset (uses global default)"
                    ),
                    handled=True,
                )
            if mode not in _MODES:
                return SlashCommandResult(
                    output=(
                        f"Usage: /details {first} "
                        f"[{ '|'.join(_MODES) }|reset]"
                    ),
                    handled=True,
                )
            sections[first] = mode
            return SlashCommandResult(
                output=f"section {first}: {mode}", handled=True
            )

        modes_help = "|".join(_MODES)
        sections_help = ", ".join(_SECTIONS)
        return SlashCommandResult(
            output=(
                "Usage:\n"
                f"  /details [{modes_help}|cycle]   # global\n"
                f"  /details <section> [{modes_help}|reset]  # per-section\n"
                f"sections: {sections_help}"
            ),
            handled=True,
        )
