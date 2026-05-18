"""``/tools`` — list the tools available to this chat (D2, gateway-vs-CLI parity).

The ``enabled_plugins`` filter silently shapes which tools a chat has;
there was no in-chat way to see them. ``/tools`` lists the registered
tool surface so a connector user knows what the agent can actually do.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class ToolsCommand(SlashCommand):
    name = "tools"
    description = "List the tools available to this chat"
    gateway_safe = True

    async def execute(
        self, args: str, runtime: RuntimeContext,
    ) -> SlashCommandResult:
        from opencomputer.tools.registry import registry

        try:
            tools = sorted(
                registry.all_tools(), key=lambda t: t.schema.name,
            )
        except Exception as exc:  # noqa: BLE001 — never raise to the user
            return SlashCommandResult(
                output=f"/tools: could not read the tool registry ({exc}).",
            )

        if not tools:
            return SlashCommandResult(output="No tools are registered.")

        lines = [f"## Tools ({len(tools)} available)"]
        for tool in tools:
            schema = tool.schema
            raw_desc = getattr(schema, "description", "") or ""
            first_line = raw_desc.strip().splitlines()[0] if raw_desc.strip() else ""
            summary = first_line[:80]
            lines.append(
                f"  • {schema.name}" + (f" — {summary}" if summary else "")
            )
        return SlashCommandResult(output="\n".join(lines))


__all__ = ["ToolsCommand"]
