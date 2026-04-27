"""TodoWrite — multi-step task tracking. Persisted to the session DB.

Uses a singleton `session_state` table keyed by session_id. Survives
`--resume` because it reads from SQLite, not memory.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

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

# Module-level default so _read_todos / _write_todos module helpers still
# work for the existing test_todos_roundtrip_for_session helper path.
# Set by the plugin's register() via api.session_db_path; tests set it
# directly. TodoWriteTool instances prefer their own self._db_path.
_default_db_path: Path | None = None


def set_default_db_path(path: Path) -> None:
    """Module-level setter used by the plugin's register() + tests."""
    global _default_db_path
    _default_db_path = Path(path)


def _resolve_db_path() -> Path:
    if _default_db_path is None:
        raise RuntimeError(
            "TodoWrite DB path not configured — the coding-harness plugin's "
            "register() should have called set_default_db_path(api.session_db_path)"
        )
    return _default_db_path


def _ensure_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(DDL)
        conn.commit()
    finally:
        conn.close()


def _read_todos(session_id: str, db_path: Path | None = None) -> list[dict]:
    path = db_path or _resolve_db_path()
    _ensure_table(path)
    conn = sqlite3.connect(path)
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


def _write_todos(session_id: str, todos: list[dict], db_path: Path | None = None) -> None:
    import time as _time

    path = db_path or _resolve_db_path()
    _ensure_table(path)
    conn = sqlite3.connect(path)
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

    def __init__(self, db_path: Path | None = None) -> None:
        # Optional instance-level override (primarily for tests); falls back
        # to the module-level _default_db_path set by plugin register().
        self._db_path: Path | None = Path(db_path) if db_path is not None else None

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="TodoWrite",
            description=(
                "Track multi-step tasks across turns. Each call REPLACES the full list "
                "(send the complete new state every time — makes changes explicit). "
                "Use for: work that requires 3+ discrete steps, complex refactors, "
                "multi-file changes, anything where the user benefits from seeing "
                "what's left. Each item has `id`, `content` (imperative: 'Add logging "
                "to utils.py'), `activeForm` (gerund: 'Adding logging to utils.py'), "
                "and `status` ∈ pending/in_progress/completed. Exactly ONE item should "
                "be in_progress at a time (soft-enforced — multiple in_progress raises "
                "an error). Update to 'completed' IMMEDIATELY after finishing a step; "
                "do NOT batch updates. Don't use for trivial 1-2 step tasks; use only "
                "when planning visibility actually helps. Persisted to the session DB "
                "so it survives --resume."
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
        _write_todos(sid, todos, db_path=getattr(self, "_db_path", None))
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


def read_todos_for_session(session_id: str, db_path: Path | None = None) -> list[dict]:
    """Public helper for tests and external tools.

    Tests typically pass ``db_path`` for isolation; callers in the running
    agent rely on the module-level default set by the plugin's register().
    """
    return _read_todos(session_id, db_path=db_path)


__all__ = ["TodoWriteTool", "read_todos_for_session"]
