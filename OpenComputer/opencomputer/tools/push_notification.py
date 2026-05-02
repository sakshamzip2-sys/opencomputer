"""PushNotification tool — fire a notification to the user via the active
channel adapter.

Routes through `BaseChannelAdapter.send_notification`. The default
implementation is the same as `send()` — every platform supports it. Adapters
that have a real push API (Telegram silent vs loud, Discord push) override
to use the proper path.

In CLI mode there is no channel adapter, so the tool prints to stdout
prefixed with `[NOTIFICATION]`. That keeps the contract honest in chat mode
without making the tool useless.

Source: claude-code's `PushNotification`, kimi's notifications subsystem.
"""

from __future__ import annotations

from opencomputer.gateway.dispatch import Dispatch
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class PushNotificationTool(BaseTool):
    parallel_safe = True
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True

    def __init__(self, dispatch: Dispatch | None = None) -> None:
        # `dispatch` is the live gateway dispatcher — supplied by the gateway
        # when the tool is registered there. None = CLI mode.
        self._dispatch = dispatch

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="PushNotification",
            description=(
                "Send a notification to the user. In gateway mode, routes via "
                "the active channel's notification API (silent on Telegram by "
                "default; pass urgent=true to override). In CLI mode, prints "
                "to stdout. Use this for background-task completion alerts and "
                "long-running results — NOT for the main reply, which the model "
                "returns normally."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Notification body. Keep short (~200 chars).",
                    },
                    "urgent": {
                        "type": "boolean",
                        "description": (
                            "If true, hint to the channel adapter to override "
                            "silent-notification mode. Default false."
                        ),
                    },
                    "chat_id": {
                        "type": "string",
                        "description": (
                            "Optional override for the destination chat. "
                            "Defaults to the current session's chat."
                        ),
                    },
                },
                "required": ["text"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        text = str(call.arguments.get("text", "")).strip()
        urgent = bool(call.arguments.get("urgent", False))
        chat_override = str(call.arguments.get("chat_id", "")).strip() or None

        if not text:
            return ToolResult(
                tool_call_id=call.id, content="Error: text is required", is_error=True
            )

        # D7: emit Notification hook so plugins can log / mirror the
        # notification elsewhere. Fire-and-forget — must not break the
        # notification flow itself.
        try:
            from opencomputer.hooks.engine import engine as _hook_engine
            from plugin_sdk.core import Message
            from plugin_sdk.hooks import HookContext, HookEvent

            _hook_engine.fire_and_forget(
                HookContext(
                    event=HookEvent.NOTIFICATION,
                    session_id="",  # push notifications are tool-scope, not session-scope
                    message=Message(role="system", content=text),
                )
            )
        except Exception:
            pass

        # CLI mode — no gateway dispatcher available.
        if self._dispatch is None:
            print(f"[NOTIFICATION{' urgent' if urgent else ''}] {text}", flush=True)
            return ToolResult(
                tool_call_id=call.id,
                content=f"Notification printed to stdout (CLI mode): {text}",
            )

        # Gateway mode — find an active adapter for the current chat.
        # Without a chat_override we can't infer destination because tools
        # don't carry chat context today. Document the limitation rather than
        # silently misbehave.
        if chat_override is None:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "Error: PushNotification needs chat_id in gateway mode. "
                    "Pass the chat id of the destination conversation."
                ),
                is_error=True,
            )

        # Use the first registered adapter — multi-channel routing belongs in
        # a follow-up.
        if not self._dispatch._adapters_by_platform:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: no channel adapters registered in gateway",
                is_error=True,
            )

        adapter = next(iter(self._dispatch._adapters_by_platform.values()))
        try:
            result = await adapter.send_notification(
                chat_override, text, urgent=urgent
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {type(e).__name__}: {e}",
                is_error=True,
            )

        if not result.success:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: send_notification failed: {result.error}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"Notification sent to {chat_override} (msg_id={result.message_id})",
        )


__all__ = ["PushNotificationTool"]
