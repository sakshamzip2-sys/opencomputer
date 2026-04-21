"""
Tool registry — a dict + dispatch.

Inspired by hermes's ToolEntry pattern. A singleton registry holds
ToolEntries; tools register themselves via `@register_tool`. The
agent loop asks the registry for all schemas and dispatches calls.
"""

from __future__ import annotations

from collections.abc import Iterable

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class ToolRegistry:
    """Singleton registry. Import from elsewhere as `from opencomputer.tools.registry import registry`."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        name = tool.schema.name
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered")
        self._tools[name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def schemas(self) -> list[ToolSchema]:
        return [t.schema for t in self._tools.values()]

    def names(self) -> Iterable[str]:
        return self._tools.keys()

    async def dispatch(self, call: ToolCall) -> ToolResult:
        """Dispatch a tool call to its handler. Never raises — always returns a ToolResult."""
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: tool '{call.name}' not found",
                is_error=True,
            )
        try:
            return await tool.execute(call)
        except Exception as e:  # defensive — tool.execute should handle its own errors
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {type(e).__name__}: {e}",
                is_error=True,
            )


registry = ToolRegistry()


def register_tool(tool: BaseTool) -> BaseTool:
    """Convenience: register and return the tool (so it can be used as a module-level call)."""
    registry.register(tool)
    return tool


__all__ = ["ToolRegistry", "registry", "register_tool"]
