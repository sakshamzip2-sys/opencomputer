"""``/history [N]`` — show the last N turns of the current session inline.

Tier 2.A.13 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Reads ``runtime.custom['session_id']`` + ``['session_db']``. Renders
the last N user/assistant pairs (default 10 messages = ~5 pairs)
without leaving the agent loop.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

DEFAULT_LIMIT = 10
MAX_LIMIT = 100
PREVIEW_CHARS = 240


def _format_message_preview(role: str, content) -> str:
    """Render a single message as ``role: <preview>`` with truncation."""
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Multimodal content blocks — extract the text parts only.
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") in ("image", "tool_use", "tool_result"):
                    parts.append(f"[{block['type']}]")
        text = " ".join(parts)
    else:
        text = str(content)
    text = text.strip()
    if len(text) > PREVIEW_CHARS:
        text = text[:PREVIEW_CHARS] + "…"
    return f"**{role}:** {text}" if text else f"**{role}:** (empty)"


class HistoryCommand(SlashCommand):
    name = "history"
    description = "Show recent turns of the current session inline"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sid = runtime.custom.get("session_id")
        db = runtime.custom.get("session_db")
        if not sid or db is None:
            return SlashCommandResult(
                output="No active session — /history only works inside an agent loop turn.",
                handled=True,
            )

        limit = DEFAULT_LIMIT
        sub = (args or "").strip()
        if sub:
            try:
                limit = int(sub)
            except ValueError:
                return SlashCommandResult(
                    output=f"Usage: /history [N]  (got {sub!r}; expected an integer)",
                    handled=True,
                )
            if limit < 1:
                limit = 1
            elif limit > MAX_LIMIT:
                limit = MAX_LIMIT

        try:
            messages = db.get_messages(sid)
        except Exception as e:  # noqa: BLE001
            return SlashCommandResult(
                output=f"Failed to read messages: {type(e).__name__}: {e}",
                handled=True,
            )

        if not messages:
            return SlashCommandResult(
                output="(no messages yet)",
                handled=True,
            )

        recent = messages[-limit:]
        lines = [f"## Last {len(recent)} of {len(messages)} messages\n"]
        for msg in recent:
            role = getattr(msg, "role", "?")
            content = getattr(msg, "content", "")
            lines.append(_format_message_preview(role, content))
            lines.append("")
        return SlashCommandResult(output="\n".join(lines), handled=True)


__all__ = ["HistoryCommand"]
