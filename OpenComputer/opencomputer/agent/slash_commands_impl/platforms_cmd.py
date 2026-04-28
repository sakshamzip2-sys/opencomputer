"""``/platforms`` — list active channel platforms.

Tier 2.A.17 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Reads from ``runtime.custom["active_platforms"]`` (list of platform
ids the gateway has paired adapters for) and renders. If the gateway
isn't running, the list is empty and we say so.

The gateway populates this each time it starts/stops an adapter; from
a CLI session with no gateway it's just empty.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class PlatformsCommand(SlashCommand):
    name = "platforms"
    description = "Show active channel platforms (gateway adapter status)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        active = runtime.custom.get("active_platforms")
        if not active:
            return SlashCommandResult(
                output=(
                    "No active channel platforms.\n"
                    "Run `oc gateway` to start the daemon and bring "
                    "configured channels online."
                ),
                handled=True,
            )

        lines = [f"## Active platforms ({len(active)})"]
        for platform in active:
            if isinstance(platform, dict):
                name = platform.get("name", "?")
                status = platform.get("status", "active")
                lines.append(f"  • {name}: {status}")
            else:
                lines.append(f"  • {platform}")
        return SlashCommandResult(output="\n".join(lines), handled=True)


__all__ = ["PlatformsCommand"]
