"""Tool router — dispatches RealtimeVoiceToolCallEvent through OC's tool registry."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest  # noqa: F401

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.realtime_voice import RealtimeVoiceToolCallEvent
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class _StubTool(BaseTool):
    parallel_safe = True

    def __init__(self) -> None:
        self.last_call: ToolCall | None = None

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(name="EchoTool", description="echo", parameters={})

    async def execute(self, call: ToolCall) -> ToolResult:
        self.last_call = call
        return ToolResult(
            tool_call_id=call.id, content=f"echoed {call.arguments}", is_error=False,
        )


def _registry_with(tool: BaseTool) -> Any:
    reg = MagicMock()
    reg.get = MagicMock(return_value=tool)
    return reg


def test_dispatch_calls_tool_and_pushes_result_back() -> None:
    from opencomputer.voice.tool_router import dispatch_realtime_tool_call

    tool = _StubTool()
    bridge = MagicMock()
    runtime = RuntimeContext()
    ev = RealtimeVoiceToolCallEvent(
        item_id="i1", call_id="c1", name="EchoTool", args={"text": "hi"},
    )

    asyncio.run(dispatch_realtime_tool_call(
        event=ev,
        registry=_registry_with(tool),
        bridge=bridge,
        runtime=runtime,
    ))

    bridge.submit_tool_result.assert_called_once()
    call_id_arg, result_arg = bridge.submit_tool_result.call_args.args
    assert call_id_arg == "c1"
    assert "echoed" in str(result_arg)
    # And the tool actually ran.
    assert tool.last_call is not None
    assert tool.last_call.id == "c1"


def test_dispatch_unknown_tool_returns_error_to_bridge() -> None:
    from opencomputer.voice.tool_router import dispatch_realtime_tool_call

    bridge = MagicMock()
    runtime = RuntimeContext()
    ev = RealtimeVoiceToolCallEvent(
        item_id="i1", call_id="c1", name="DoesNotExist", args={},
    )
    registry = MagicMock(get=MagicMock(return_value=None))

    asyncio.run(dispatch_realtime_tool_call(
        event=ev, registry=registry, bridge=bridge, runtime=runtime,
    ))

    bridge.submit_tool_result.assert_called_once()
    _cid, result = bridge.submit_tool_result.call_args.args
    assert "unknown tool" in str(result).lower() or "not found" in str(result).lower()


def test_dispatch_in_plan_mode_refuses_tools() -> None:
    """In PLAN mode, the router refuses tools without setting them off."""
    from opencomputer.voice.tool_router import dispatch_realtime_tool_call

    tool = _StubTool()
    bridge = MagicMock()
    runtime = RuntimeContext(plan_mode=True)
    ev = RealtimeVoiceToolCallEvent(
        item_id="i1", call_id="c1", name="EchoTool", args={"text": "hi"},
    )

    asyncio.run(dispatch_realtime_tool_call(
        event=ev,
        registry=_registry_with(tool),
        bridge=bridge,
        runtime=runtime,
    ))

    bridge.submit_tool_result.assert_called_once()
    _cid, result = bridge.submit_tool_result.call_args.args
    assert "plan" in str(result).lower() or "refused" in str(result).lower()
    # Tool was NOT executed.
    assert tool.last_call is None
