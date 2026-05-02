"""SessionSearchTool — LLM-callable wrapper over SessionDB.search_messages.

Returns up to `limit` FTS5 hits as a compact text block. Use when the agent
needs to recall facts from prior conversations within the user's session history.
"""
from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_DEFAULT_LIMIT = 10
_BODY_PREVIEW = 200


def _resolve_default_db() -> Any:
    """Lazily resolve the default SessionDB so CLI registration needs no args."""
    from opencomputer.agent.config import default_config
    from opencomputer.agent.state import SessionDB

    cfg = default_config()
    return SessionDB(cfg.session.db_path)


class SessionSearchTool(BaseTool):
    parallel_safe = True

    def __init__(self, db: Any = None) -> None:
        self._db = db if db is not None else _resolve_default_db()

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SessionSearch",
            description=(
                "Full-text search across the user's prior conversations. Returns "
                "matching message snippets from any session."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(
                tool_call_id=call.id,
                content="missing required argument: query",
                is_error=True,
            )

        limit = args.get("limit", _DEFAULT_LIMIT)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        limit = max(1, min(50, limit))

        try:
            hits = self._db.search_messages(query, limit=limit)
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id,
                content=f"search failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        if not hits:
            return ToolResult(tool_call_id=call.id, content=f"No matches for '{query}'.")

        lines = [f"Found {len(hits)} match(es) for '{query}':", ""]
        for h in hits:
            sid = (h.get("session_id") or "")[:8]
            role = h.get("role") or "?"
            body = h.get("content") or h.get("body") or h.get("snippet") or ""
            preview = body[:_BODY_PREVIEW] + ("…" if len(body) > _BODY_PREVIEW else "")
            lines.append(f"[{sid}…] {role}: {preview}")
        return ToolResult(tool_call_id=call.id, content="\n".join(lines))


__all__ = ["SessionSearchTool"]
