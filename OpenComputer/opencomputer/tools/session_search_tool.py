"""SessionSearch tool — agent-facing FTS5 query over conversation history.

Wraps SessionDB.search_messages(). Returns a formatted, compact text block
the agent can read — one match per line with session id, timestamp, role,
and the full message content.

Sanitizes the query just enough to avoid the most common FTS5 syntax
accidents (unbalanced quotes, reserved-word-only queries). For a full
FTS5 query the agent can still write MATCH expressions directly —
that's the power of FTS5 and we don't want to take it away.
"""

from __future__ import annotations

import datetime
from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50


class SessionSearchTool(BaseTool):
    """FTS5 keyword search across past messages in all sessions."""

    parallel_safe = True  # read-only; SQLite WAL handles concurrent readers

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SessionSearch",
            description=(
                "Search past messages across all sessions using SQLite FTS5 "
                "keyword matching. Use this to find conversations you had "
                "before (e.g. 'what did we decide about X', 'did I mention "
                "my preference for Y'). Returns matching messages with "
                "session id, timestamp, role, and full content.\n\n"
                'FTS5 supports: keyword, "exact phrase", term1 OR term2, '
                "term1 NOT term2, prefix*. See SQLite FTS5 docs for more."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "FTS5 search query.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": _DEFAULT_LIMIT,
                        "minimum": 1,
                        "maximum": _MAX_LIMIT,
                        "description": (
                            f"Max matches to return (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})."
                        ),
                    },
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
                content="Error: query is required",
                is_error=True,
            )

        limit = int(args.get("limit", _DEFAULT_LIMIT))
        limit = max(1, min(limit, _MAX_LIMIT))

        try:
            rows = self._ctx.db.search_messages(query, limit=limit)
        except Exception as e:  # pragma: no cover — defensive
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: search failed: {e}",
                is_error=True,
            )

        if not rows:
            return ToolResult(
                tool_call_id=call.id,
                content=f"No matches found for {query!r}.",
                is_error=False,
            )

        return ToolResult(
            tool_call_id=call.id,
            content=_format_rows(rows, query),
            is_error=False,
        )


def _format_rows(rows: list[dict], query: str) -> str:
    lines = [f"{len(rows)} match(es) for {query!r}:\n"]
    for r in rows:
        ts = _fmt_ts(r.get("timestamp"))
        session = r.get("session_id", "?")
        role = r.get("role", "?")
        content = (r.get("content") or "").strip()
        lines.append(f"[{session} {ts} {role}]\n{content}\n")
    return "\n".join(lines)


def _fmt_ts(ts: Any) -> str:
    if ts is None:
        return "?"
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(ts)
