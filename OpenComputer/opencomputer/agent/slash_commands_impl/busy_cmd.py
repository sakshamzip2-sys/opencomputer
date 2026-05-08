"""``/busy [interrupt|queue|steer|status]`` — busy-input mode setter.

Hermes-CLI parity (doc lines 155-176). Replaces ``/queue-mode``, which
remains as a deprecation alias.

Modes:

- ``interrupt`` (default) — message cancels the current operation.
- ``queue``               — message queued for next turn.
- ``steer``               — message injected via the steer subsystem
                            after the next tool call (falls back to
                            ``queue`` if no tool call this turn).
- ``status``              — print current mode + describe each.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_MODES = ("interrupt", "queue", "steer")


def _describe() -> str:
    return (
        "interrupt — cancel current operation\n"
        "queue     — queue silently for next turn\n"
        "steer     — inject after next tool call (fallback: queue)\n"
        "status    — show current mode"
    )


class BusyCommand(SlashCommand):
    name = "busy"
    description = "Busy-input mode (interrupt/queue/steer/status)."

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("busy_input_mode", "interrupt")

        if sub in _MODES:
            runtime.custom["busy_input_mode"] = sub
            return SlashCommandResult(
                output=f"busy-input mode: {sub}", handled=True
            )
        if sub in ("", "status"):
            return SlashCommandResult(
                output=f"current: {current}\n\n{_describe()}",
                handled=True,
            )
        modes_help = "|".join(_MODES)
        return SlashCommandResult(
            output=f"Usage: /busy [{modes_help}|status]\n\n{_describe()}",
            handled=True,
        )
