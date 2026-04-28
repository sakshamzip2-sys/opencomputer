"""``/title [name]`` — get or set the current session's title.

Tier 2.A.11 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Reads ``runtime.custom['session_id']`` + ``['session_db']`` (plumbed
into runtime by the agent loop right before slash dispatch).

Usage:
    /title              → show current title
    /title my-debug     → set title to "my-debug"
    /title ""           → clear title (back to auto-generated)
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class TitleCommand(SlashCommand):
    name = "title"
    description = "Get or set the current session's title"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sid = runtime.custom.get("session_id")
        db = runtime.custom.get("session_db")
        if not sid or db is None:
            return SlashCommandResult(
                output="No active session — /title only works inside an agent loop turn.",
                handled=True,
            )

        title = (args or "").strip()

        # No arg → show current title
        if title == "":
            current = db.get_session_title(sid)
            if current:
                return SlashCommandResult(
                    output=f"Current title: {current}",
                    handled=True,
                )
            return SlashCommandResult(
                output="(no title set; auto-generated when conversation matures)",
                handled=True,
            )

        # Length cap to avoid pathological input
        if len(title) > 200:
            return SlashCommandResult(
                output=f"title too long ({len(title)} chars); cap is 200",
                handled=True,
            )

        db.set_session_title(sid, title)
        return SlashCommandResult(
            output=f"Session titled: {title}",
            handled=True,
        )


__all__ = ["TitleCommand"]
