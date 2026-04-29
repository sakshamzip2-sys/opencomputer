"""Tool dispatch for realtime voice.

When the bridge emits a tool call, look up the tool in OC's
``ToolRegistry``, dispatch it (async), then push the result back via
``bridge.submit_tool_result``. Honors ``effective_permission_mode``:

* ``PermissionMode.PLAN`` — refuse the call with a "plan mode" string.
* ``PermissionMode.AUTO`` — auto-approve.
* ``PermissionMode.DEFAULT`` — execute (consent gate is the real
  enforcement layer; this router doesn't re-prompt because there's
  no terminal in voice mode).
"""
from __future__ import annotations

from typing import Any, Protocol
from uuid import uuid4

from plugin_sdk.core import ToolCall
from plugin_sdk.permission_mode import PermissionMode, effective_permission_mode
from plugin_sdk.realtime_voice import RealtimeVoiceToolCallEvent
from plugin_sdk.runtime_context import RuntimeContext


class _Bridge(Protocol):
    def submit_tool_result(self, call_id: str, result: Any) -> None: ...


class _Registry(Protocol):
    def get(self, name: str) -> Any: ...


async def dispatch_realtime_tool_call(
    *,
    event: RealtimeVoiceToolCallEvent,
    registry: _Registry,
    bridge: _Bridge,
    runtime: RuntimeContext,
) -> None:
    """Run the tool referenced by ``event`` and push the result to ``bridge``.

    Errors are swallowed into the result string — never raised — so a
    bad tool call doesn't kill the voice session.
    """
    mode = effective_permission_mode(runtime)
    if mode == PermissionMode.PLAN:
        bridge.submit_tool_result(event.call_id, {
            "error": (
                "Tool call refused — agent is in plan mode. "
                f"({event.name} would have run with {event.args!r})"
            ),
        })
        return

    tool = registry.get(event.name)
    if tool is None:
        bridge.submit_tool_result(event.call_id, {
            "error": f"unknown tool: {event.name!r}",
        })
        return

    call = ToolCall(
        id=event.call_id or str(uuid4()),
        name=event.name,
        arguments=event.args if isinstance(event.args, dict) else {},
    )
    try:
        result = await tool.execute(call)
    except Exception as exc:  # noqa: BLE001 — never crash the session
        bridge.submit_tool_result(event.call_id, {"error": str(exc)})
        return

    payload: Any
    if hasattr(result, "content") and hasattr(result, "is_error"):
        payload = {"content": result.content, "is_error": result.is_error}
    else:
        payload = result
    bridge.submit_tool_result(event.call_id, payload)


__all__ = ["dispatch_realtime_tool_call"]
