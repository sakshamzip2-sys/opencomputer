"""TodoWrite — multi-step task tracking. Persisted to the session DB.

Uses a singleton `session_state` table keyed by session_id. Survives
`--resume` because it reads from SQLite, not memory.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

import opencomputer.agent.config as _cfg_mod
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


DDL = """
CREATE TABLE IF NOT EXISTS session_state (
    session_id TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, key)
);
"""


def _db_path() -> Path:
    # Dynamic lookup — tests patch _cfg_mod.default_config to redirect the DB.
    return _cfg_mod.default_config().session.db_path


def _ensure_table() -> None:
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute(DDL)
        conn.commit()
    finally:
        conn.close()


def _read_todos(session_id: str) -> list[dict]:
    _ensure_table()
    conn = sqlite3.connect(_db_path())
    try:
        row = conn.execute(
            "SELECT value FROM session_state WHERE session_id=? AND key='todos'",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return []
    try:
        return json.loads(row[0]) or []
    except Exception:
        return []


def _write_todos(session_id: str, todos: list[dict]) -> None:
    import time as _time

    _ensure_table()
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute(
            "INSERT OR REPLACE INTO session_state(session_id, key, value, updated_at) "
            "VALUES (?, 'todos', ?, ?)",
            (session_id, json.dumps(todos), _time.time()),
        )
        conn.commit()
    finally:
        conn.close()


TodoStatus = Literal["pending", "in_progress", "completed"]


class TodoWriteTool(BaseTool):
    """Each tool call fully REPLACES the todo list (Claude Code shape).

    The agent passes the complete new list every time, which makes state
    changes explicit and easier to verify. Rules (soft-enforced in schema):
    - at most one item in_progress at a time
    - `content` describes the task (imperative), `activeForm` is the gerund ("Running tests")
    """

    parallel_safe = False  # DB writes

    # The session_id the tool writes against. Set by the agent loop at
    # runtime via a class-level setter (similar to DelegateTool).
    _session_id: str = ""

    @classmethod
    def set_session_id(cls, session_id: str) -> None:
        cls._session_id = session_id

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="TodoWrite",
            description=(
                "Track multi-step tasks across turns. Each call REPLACES the full list. "
                "Use for: work requiring 3+ discrete steps, complex refactors, multi-file "
                "changes. Exactly ONE item should be in_progress at a time. Update to "
                "'completed' IMMEDIATELY after finishing a step; do not batch."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "Complete replacement list of todos.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {
                                    "type": "string",
                                    "description": "Imperative: 'Add logging to utils.py'",
                                },
                                "activeForm": {
                                    "type": "string",
                                    "description": "Gerund: 'Adding logging to utils.py'",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                            },
                            "required": ["id", "content", "status"],
                        },
                    },
                },
                "required": ["todos"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        todos = call.arguments.get("todos", [])
        if not isinstance(todos, list):
            return ToolResult(
                tool_call_id=call.id,
                content="Error: todos must be a list",
                is_error=True,
            )
        # Soft enforce: only one in_progress
        in_prog = [t for t in todos if t.get("status") == "in_progress"]
        if len(in_prog) > 1:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: {len(in_prog)} items are in_progress. "
                    f"Only one may be active at a time."
                ),
                is_error=True,
            )
        sid = self._session_id or "default"
        _write_todos(sid, todos)
        counts = {
            "pending": sum(1 for t in todos if t.get("status") == "pending"),
            "in_progress": sum(1 for t in todos if t.get("status") == "in_progress"),
            "completed": sum(1 for t in todos if t.get("status") == "completed"),
        }
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"Todos updated ({len(todos)} total: "
                f"{counts['pending']} pending, "
                f"{counts['in_progress']} in_progress, "
                f"{counts['completed']} completed)"
            ),
        )


def read_todos_for_session(session_id: str) -> list[dict]:
    """Public helper for tests and external tools."""
    return _read_todos(session_id)


__all__ = ["TodoWriteTool", "read_todos_for_session"]
