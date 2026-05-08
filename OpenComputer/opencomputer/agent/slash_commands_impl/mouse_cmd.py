"""``/mouse [on|off|toggle|status]`` — toggle TUI mouse tracking.

Hermes-CLI parity (doc line 287). Writes ``runtime.custom["mouse_tracking"]``;
the TUI render layer consumes the flag on next sync to enable/disable the
raw-mouse mode.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class MouseCommand(SlashCommand):
    name = "mouse"
    description = "Toggle TUI mouse tracking (on/off/toggle/status)."

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        cur = bool(runtime.custom.get("mouse_tracking", True))
        if sub in ("", "toggle"):
            new = not cur
        elif sub == "on":
            new = True
        elif sub == "off":
            new = False
        elif sub == "status":
            return SlashCommandResult(
                output=f"mouse tracking: {'ON' if cur else 'OFF'}",
                handled=True,
            )
        else:
            return SlashCommandResult(
                output="Usage: /mouse [on|off|toggle|status]", handled=True
            )
        runtime.custom["mouse_tracking"] = new
        return SlashCommandResult(
            output=f"mouse tracking: {'ON' if new else 'OFF'}", handled=True
        )
