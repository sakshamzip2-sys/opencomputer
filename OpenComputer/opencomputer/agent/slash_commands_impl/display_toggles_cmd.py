"""``/verbose`` and ``/statusbar`` — display toggle slash commands.

Tier 2.A.21 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Both store state in ``runtime.custom``. The TUI renderer reads these
keys when rendering — full integration is out of scope here, but the
storage knob is in place.

/verbose cycles four modes: off → new → all → verbose → off
/statusbar toggles bool: on → off → on
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_VERBOSE_MODES: tuple[str, ...] = ("off", "new", "all", "verbose")


class VerboseCommand(SlashCommand):
    name = "verbose"
    description = "Cycle tool-progress display mode (off/new/all/verbose)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("tool_progress", "new")

        if sub in _VERBOSE_MODES:
            runtime.custom["tool_progress"] = sub
            return SlashCommandResult(
                output=f"tool progress mode: {sub}", handled=True,
            )
        if sub in ("", "next", "cycle"):
            try:
                idx = _VERBOSE_MODES.index(current)
            except ValueError:
                idx = -1
            new_mode = _VERBOSE_MODES[(idx + 1) % len(_VERBOSE_MODES)]
            runtime.custom["tool_progress"] = new_mode
            return SlashCommandResult(
                output=f"tool progress mode: {new_mode}", handled=True,
            )
        if sub == "status":
            return SlashCommandResult(
                output=f"tool progress mode: {current}", handled=True,
            )
        return SlashCommandResult(
            output=(
                f"Usage: /verbose [{ '|'.join(_VERBOSE_MODES) }|next|status]\n"
                "  off / new / all / verbose — set explicit mode\n"
                "  next or no arg — cycle to next mode\n"
                "  status — report without changing"
            ),
            handled=True,
        )


class StatusbarCommand(SlashCommand):
    name = "statusbar"
    description = "Toggle the TUI status bar"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = bool(runtime.custom.get("statusbar", True))

        if sub in ("", "toggle"):
            new_state = not current
        elif sub == "on":
            new_state = True
        elif sub == "off":
            new_state = False
        elif sub == "status":
            return SlashCommandResult(
                output=f"statusbar: {'ON' if current else 'OFF'}", handled=True,
            )
        else:
            return SlashCommandResult(
                output="Usage: /statusbar [on|off|toggle|status]", handled=True,
            )

        runtime.custom["statusbar"] = new_state
        return SlashCommandResult(
            output=f"statusbar: {'ON' if new_state else 'OFF'}", handled=True,
        )


__all__ = ["VerboseCommand", "StatusbarCommand"]
