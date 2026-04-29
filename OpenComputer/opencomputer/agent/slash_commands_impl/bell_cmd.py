"""``/bell [on|off|toggle|status]`` — toggle terminal-bell-on-complete.

Tier 2.B from docs/refs/hermes-agent/2026-04-28-major-gaps.md. Sets
``runtime.custom['bell_on_complete']``; the chat loop's
``maybe_emit_bell()`` reads it after each turn finishes.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class BellCommand(SlashCommand):
    name = "bell"
    description = "Toggle terminal bell on turn-complete"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = bool(runtime.custom.get("bell_on_complete", False))

        if sub in ("", "toggle"):
            new_state = not current
        elif sub == "on":
            new_state = True
        elif sub == "off":
            new_state = False
        elif sub == "status":
            return SlashCommandResult(
                output=f"bell on complete: {'ON' if current else 'OFF'}",
                handled=True,
            )
        else:
            return SlashCommandResult(
                output="Usage: /bell [on|off|toggle|status]",
                handled=True,
            )

        runtime.custom["bell_on_complete"] = new_state
        return SlashCommandResult(
            output=f"bell on complete: {'ON' if new_state else 'OFF'}",
            handled=True,
        )


__all__ = ["BellCommand"]
