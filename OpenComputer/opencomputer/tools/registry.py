"""
Tool registry — a dict + dispatch.

Inspired by hermes's ToolEntry pattern. A singleton registry holds
ToolEntries; tools register themselves via `@register_tool`. The
agent loop asks the registry for all schemas and dispatches calls.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

logger = logging.getLogger("opencomputer.tools.registry")


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

    async def dispatch(
        self,
        call: ToolCall,
        *,
        session_id: str | None = None,
        turn_index: int | None = None,
        demand_tracker: Any | None = None,
    ) -> ToolResult:
        """Dispatch a tool call to its handler. Never raises — always returns a ToolResult.

        Phase 12b.5 Task E3: on the tool-not-found path, if a demand
        tracker is provided (duck-typed — we use ``Any`` to avoid the
        ``opencomputer.plugins.demand_tracker`` import cycle), record the
        miss best-effort. Exceptions from the tracker are swallowed so
        dispatch never fails because of demand-tracking infrastructure.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            if demand_tracker is not None and session_id is not None:
                try:
                    demand_tracker.record_tool_not_found(
                        call.name,
                        session_id,
                        turn_index or 0,
                    )
                except Exception:  # noqa: BLE001
                    # Best-effort — never let the demand tracker break dispatch.
                    logger.debug(
                        "demand_tracker.record_tool_not_found raised; swallowing",
                        exc_info=True,
                    )
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
