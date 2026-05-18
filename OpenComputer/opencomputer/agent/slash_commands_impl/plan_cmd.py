"""``/plan on|off|status`` — per-chat plan mode (A2, gateway-vs-CLI parity).

The CLI starts a turn in plan mode via ``oc --plan``; the gateway had no
equivalent, so every Telegram/Discord message was full-execute. This
command toggles a per-chat plan-mode flag persisted in
``<profile>/gateway/runtime_state.json``. The gateway dispatcher reads
the flag each turn and threads ``plan_mode`` onto the ``RuntimeContext``.

Usage::

    /plan on        # outline before acting; Edit/Write/Bash refused
    /plan off       # back to normal execution
    /plan           # (or /plan status) show the current setting
"""

from __future__ import annotations

from opencomputer.gateway.runtime_state import get_runtime_state
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_ON = {"on", "enable", "enabled", "true", "1"}
_OFF = {"off", "disable", "disabled", "false", "0"}
_STATUS = {"", "status", "show"}


class PlanCommand(SlashCommand):
    name = "plan"
    description = "Toggle plan mode for this chat — /plan on|off|status"
    # A2 — runs inline on the gateway; bypass lets it land mid-turn.
    gateway_safe = True
    bypass_running_guard = True

    async def execute(
        self, args: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        custom = runtime.custom or {}
        session_id = custom.get("session_id")
        if not session_id:
            return SlashCommandResult(
                output="/plan: no session context — try again in a chat.",
            )

        arg = (args or "").strip().lower()
        store = get_runtime_state()

        if arg in _ON:
            store.set_plan_mode(session_id, True)
            return SlashCommandResult(
                output=(
                    "🗂️ Plan mode **ON** — I'll outline my approach before "
                    "acting; Edit / Write / Bash are held back. Turn off "
                    "with `/plan off`."
                ),
            )
        if arg in _OFF:
            store.set_plan_mode(session_id, False)
            return SlashCommandResult(
                output="Plan mode **OFF** — back to normal execution.",
            )
        if arg in _STATUS:
            current = store.get_plan_mode(session_id)
            state = "ON" if current else "OFF"
            return SlashCommandResult(
                output=(
                    f"Plan mode is **{state}** for this chat. "
                    f"Use `/plan on` or `/plan off` to change it."
                ),
            )
        return SlashCommandResult(
            output=f"/plan: unknown option {arg!r}. Use on | off | status.",
        )


__all__ = ["PlanCommand"]
