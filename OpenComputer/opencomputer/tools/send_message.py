"""SendMessageTool — first-class tool for cross-platform agent-driven sends.

Tier 1.B Tool 1 (per docs/refs/hermes-agent/2026-04-28-major-gaps.md).

Promotes the existing MCP ``messages_send`` capability into a core tool so
the agent reaches for it by reflex (same model the gap audit flagged: "OC has
the capability but the agent doesn't reach for it" because it was buried
behind MCP).

Architecture: wraps the same ``OutgoingQueue`` write path the MCP tool uses
— the gateway daemon drains the queue and dispatches via the live channel
adapter. No synchronous delivery guarantee.
"""

from __future__ import annotations

import json
from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# Hard limit on body size — anything larger is almost certainly a bug
# (channels themselves cap around 4-10K). Reject loudly rather than
# truncate silently.
MAX_BODY_CHARS = 10_000


class SendMessageTool(BaseTool):
    """Send a message to a configured platform channel via the gateway."""

    parallel_safe = True  # SQLite enqueue is serialized internally

    def __init__(self, db_path: Path | None = None) -> None:
        """`db_path` overrides the sessions.db location (test injection).

        Default: lazy-resolve to ``<profile_home>/sessions.db`` at
        execute() time so the tool works in any active profile.
        """
        self._db_path_override = db_path

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SendMessage",
            description=(
                "Send a message to a configured channel (telegram/discord/slack/etc.) "
                "via the gateway daemon. Writes to the outgoing-message queue; the "
                "gateway picks it up and dispatches via the live adapter. Returns "
                "the queue id immediately — does NOT wait for delivery confirmation. "
                "Use this when the agent needs to proactively notify a user on a "
                "platform they aren't currently chatting with (cron output, "
                "cross-platform handoff, scheduled reminders)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "description": (
                            "Platform identifier — 'telegram', 'discord', 'slack', "
                            "'whatsapp', 'signal', 'email', 'matrix', 'mattermost', "
                            "'imessage', 'sms', 'webhook', 'homeassistant'. Must "
                            "match a paired adapter on the gateway."
                        ),
                    },
                    "chat_id": {
                        "type": "string",
                        "description": (
                            "Platform-native chat/channel/DM identifier as a "
                            "string. Telegram chat id, Discord channel id, etc."
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "Message text (plaintext). Adapter handles "
                            "platform-native formatting."
                        ),
                    },
                    "thread_hint": {
                        "type": "string",
                        "description": (
                            "Optional topic tag. Replies preserving this hint "
                            "derive a SEPARATE OpenComputer session from the same "
                            "chat — useful for cron output that shouldn't pollute "
                            "an interactive session."
                        ),
                    },
                },
                "required": ["platform", "chat_id", "body"],
            },
        )

    def _resolve_db_path(self) -> Path:
        if self._db_path_override is not None:
            return self._db_path_override
        # Lazy import — avoids pulling in profile system at module-load time
        # so tests that pass db_path explicitly never touch the real profile.
        from opencomputer.agent.config import _home

        return _home() / "sessions.db"

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        platform = args.get("platform", "").strip()
        chat_id = args.get("chat_id", "").strip() if isinstance(args.get("chat_id"), str) else str(args.get("chat_id") or "")
        body = args.get("body", "")
        thread_hint = args.get("thread_hint")

        if not platform:
            return ToolResult(
                tool_call_id=call.id,
                content="missing required argument: platform",
                is_error=True,
            )
        if not chat_id:
            return ToolResult(
                tool_call_id=call.id,
                content="missing required argument: chat_id",
                is_error=True,
            )
        if not body or not str(body).strip():
            return ToolResult(
                tool_call_id=call.id,
                content="missing or empty required argument: body",
                is_error=True,
            )
        if len(body) > MAX_BODY_CHARS:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"body too long: {len(body)} chars exceeds limit of "
                    f"{MAX_BODY_CHARS}. Split into multiple SendMessage calls "
                    "or summarize before sending."
                ),
                is_error=True,
            )

        try:
            from opencomputer.gateway.outgoing_queue import OutgoingQueue

            queue = OutgoingQueue(self._resolve_db_path())
            metadata: dict = {}
            if thread_hint:
                metadata["thread_hint"] = thread_hint
            msg = queue.enqueue(
                platform=platform,
                chat_id=chat_id,
                body=body,
                metadata=metadata,
            )
        except Exception as e:  # noqa: BLE001 — never raise from tools
            return ToolResult(
                tool_call_id=call.id,
                content=f"failed to enqueue message: {type(e).__name__}: {e}",
                is_error=True,
            )

        payload = {
            "id": msg.id,
            "status": msg.status,
            "platform": platform,
            "chat_id": chat_id,
            "thread_hint": thread_hint,
            "note": (
                "Queued for delivery. Gateway daemon drains every ~1s; if no "
                "gateway is running the message waits indefinitely. Check "
                "delivery state via the OutgoingQueue or the gateway logs."
            ),
        }
        return ToolResult(
            tool_call_id=call.id,
            content=json.dumps(payload, indent=2),
        )


__all__ = ["SendMessageTool"]
