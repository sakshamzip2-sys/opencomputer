"""Kanban tools — agent-facing surface for worker sessions (Wave 6.B).

Hermes-port (c86842546). Each tool wraps one of the verbatim hermes
handlers in :mod:`opencomputer.tools._kanban_handlers` as a BaseTool
subclass that integrates with OC's tool registry + dispatch path.

Tools are gated on ``OC_KANBAN_TASK`` env: only sessions spawned by the
kanban dispatcher (or with the env explicitly set) see them. A normal
``oc chat`` session has zero kanban tools in its schema.
"""

from __future__ import annotations

import os
from typing import Any

from opencomputer.tools._kanban_handlers import (
    KANBAN_BLOCK_SCHEMA,
    KANBAN_COMMENT_SCHEMA,
    KANBAN_COMPLETE_SCHEMA,
    KANBAN_CREATE_SCHEMA,
    KANBAN_HEARTBEAT_SCHEMA,
    KANBAN_LINK_SCHEMA,
    KANBAN_SHOW_SCHEMA,
    _handle_block,
    _handle_comment,
    _handle_complete,
    _handle_create,
    _handle_heartbeat,
    _handle_link,
    _handle_show,
)
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


def _is_kanban_session() -> bool:
    """Tool gate: kanban tools only show up when OC_KANBAN_TASK is set."""
    return bool(os.environ.get("OC_KANBAN_TASK"))


def _tool_schema(spec: dict[str, Any]) -> ToolSchema:
    """Convert hermes' dict-schema to OC's ToolSchema dataclass."""
    return ToolSchema(
        name=spec["name"],
        description=spec.get("description", ""),
        parameters=spec.get("parameters", {}),
    )


def _result(text: str) -> ToolResult:
    """Wrap a handler's JSON-string return as a ToolResult."""
    return ToolResult(tool_call_id="", content=text, is_error=False)


class _KanbanToolBase(BaseTool):
    """Common scaffolding: gate on OC_KANBAN_TASK + delegate to handler."""

    _SCHEMA: dict[str, Any] = {}
    _HANDLER = staticmethod(lambda args, **kw: "")  # type: ignore[assignment]

    @property
    def schema(self) -> ToolSchema:
        return _tool_schema(self._SCHEMA)

    async def execute(self, call: ToolCall) -> ToolResult:
        if not _is_kanban_session():
            return ToolResult(
                tool_call_id=call.id if hasattr(call, "id") else "",
                content='{"error":"kanban tools available only when OC_KANBAN_TASK is set"}',
                is_error=True,
            )
        try:
            args = call.arguments or {}
            text = type(self)._HANDLER(args)
            return ToolResult(
                tool_call_id=call.id if hasattr(call, "id") else "",
                content=text,
                is_error='"error"' in (text or "")[:64],
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id if hasattr(call, "id") else "",
                content=f'{{"error":"{type(exc).__name__}: {exc}"}}',
                is_error=True,
            )


class KanbanShowTool(_KanbanToolBase):
    _SCHEMA = KANBAN_SHOW_SCHEMA
    _HANDLER = staticmethod(_handle_show)


class KanbanCompleteTool(_KanbanToolBase):
    _SCHEMA = KANBAN_COMPLETE_SCHEMA
    _HANDLER = staticmethod(_handle_complete)


class KanbanBlockTool(_KanbanToolBase):
    _SCHEMA = KANBAN_BLOCK_SCHEMA
    _HANDLER = staticmethod(_handle_block)


class KanbanHeartbeatTool(_KanbanToolBase):
    _SCHEMA = KANBAN_HEARTBEAT_SCHEMA
    _HANDLER = staticmethod(_handle_heartbeat)


class KanbanCommentTool(_KanbanToolBase):
    _SCHEMA = KANBAN_COMMENT_SCHEMA
    _HANDLER = staticmethod(_handle_comment)


class KanbanCreateTool(_KanbanToolBase):
    _SCHEMA = KANBAN_CREATE_SCHEMA
    _HANDLER = staticmethod(_handle_create)


class KanbanLinkTool(_KanbanToolBase):
    _SCHEMA = KANBAN_LINK_SCHEMA
    _HANDLER = staticmethod(_handle_link)


__all__ = [
    "KanbanBlockTool",
    "KanbanCommentTool",
    "KanbanCompleteTool",
    "KanbanCreateTool",
    "KanbanHeartbeatTool",
    "KanbanLinkTool",
    "KanbanShowTool",
]
