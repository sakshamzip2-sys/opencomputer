"""``/fast [normal|fast|status]`` — toggle priority service tier.

Tier 2.A.9 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Sets ``runtime.custom["service_tier"]`` so provider plugins can request
the priority/fast tier (Anthropic Fast Mode, OpenAI Priority). Higher
cost, lower latency. Useful for live demos and time-sensitive tasks.

Subcommands:
    /fast            → toggle on/off
    /fast on / fast  → enable priority tier
    /fast off / normal → disable (default tier)
    /fast status     → report without changing
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_USAGE = (
    "Usage: /fast [on|off|fast|normal|status]\n"
    "  fast / on    — request priority service tier (higher cost, lower latency)\n"
    "  normal / off — default tier\n"
    "  status (or no arg) — show current state"
)


class FastCommand(SlashCommand):
    name = "fast"
    description = "Toggle priority service tier (Anthropic Fast / OpenAI Priority)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("service_tier", "default")
        is_fast = current == "priority"

        if sub == "":
            new_state = not is_fast
        elif sub in ("on", "fast"):
            new_state = True
        elif sub in ("off", "normal"):
            new_state = False
        elif sub == "status":
            return SlashCommandResult(
                output=f"service tier: {current}",
                handled=True,
            )
        else:
            return SlashCommandResult(output=_USAGE, handled=True)

        runtime.custom["service_tier"] = "priority" if new_state else "default"
        msg = (
            "service tier set to PRIORITY (higher cost, lower latency)"
            if new_state
            else "service tier set to default"
        )
        return SlashCommandResult(output=msg, handled=True)


__all__ = ["FastCommand"]
