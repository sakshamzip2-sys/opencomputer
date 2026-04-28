"""Sessions read trio — agent-facing window into ``SessionDB``.

Sub-project 1.F-read of the OpenClaw Tier 1 port (2026-04-28). Read-only
surface only — Spawn / Send sub-agent tools are deliberately deferred
(see ``docs/superpowers/plans/2026-04-28-openclaw-tier1-port-AMENDMENTS.md``).

Three tools, one delegating direct call each:

- ``SessionsList``     → ``SessionDB.list_sessions(limit)``
- ``SessionsHistory``  → ``SessionDB.get_messages(session_id)`` (sliced client-side)
- ``SessionsStatus``   → ``SessionDB.get_session(session_id)``

These are local-SQL reads with no consent gating: matches the policy of
every other read-only tool in the bundle. ``capability_claims`` stays
empty so the F1 ConsentGate is a no-op for them.
"""

from __future__ import annotations

from opencomputer.agent.config import default_config
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

DEFAULT_LIST_LIMIT = 20
DEFAULT_HISTORY_LIMIT = 30


def _resolve_default_db() -> SessionDB:
    """Mirror :class:`opencomputer.tools.recall.RecallTool`'s init pattern:
    fall back to the user's configured ``SessionDB`` so the bundled CLI
    can register these tools without explicit wiring. Tests pass an
    explicit ``db`` for isolation."""
    cfg = default_config()
    return SessionDB(cfg.session.db_path)


class SessionsList(BaseTool):
    """List recent sessions for the current profile."""

    parallel_safe = True

    def __init__(self, db: SessionDB | None = None) -> None:
        self._db = db if db is not None else _resolve_default_db()

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SessionsList",
            description=(
                "List recent sessions for the current profile, ordered by "
                "last-activity timestamp (newest first). Returns session rows "
                "with id, title, platform, model, message_count, input_tokens, "
                "output_tokens, vibe, created_at, last_active_at. Use this to "
                "enumerate sessions before calling SessionsHistory or "
                "SessionsStatus on a specific id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Maximum number of sessions to return, ordered "
                            f"newest-first. Default {DEFAULT_LIST_LIMIT}."
                        ),
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        limit = int(call.arguments.get("limit", DEFAULT_LIST_LIMIT))
        rows = self._db.list_sessions(limit=limit)
        return ToolResult(tool_call_id=call.id, content=str(rows), is_error=False)


class SessionsHistory(BaseTool):
    """Read recent messages from a session, sliced to ``limit`` from the end."""

    parallel_safe = True

    def __init__(self, db: SessionDB | None = None) -> None:
        self._db = db if db is not None else _resolve_default_db()

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SessionsHistory",
            description=(
                "Read the most recent messages from a specific session by id. "
                "Returns up to ``limit`` messages (default 30) with role, "
                "content, and tool_call/tool_result fields. Use after "
                "SessionsList to fetch the actual conversation content of a "
                "prior session — useful for quoting earlier turns or recalling "
                "context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session id to fetch messages from.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Number of trailing messages to keep "
                            f"(client-side slice). Default {DEFAULT_HISTORY_LIMIT}."
                        ),
                    },
                },
                "required": ["session_id"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        sid = call.arguments["session_id"]
        limit = int(call.arguments.get("limit", DEFAULT_HISTORY_LIMIT))
        msgs = self._db.get_messages(sid)  # NB: SessionDB.get_messages takes no limit
        msgs = msgs[-limit:]                # slice client-side
        return ToolResult(tool_call_id=call.id, content=str(msgs), is_error=False)


class SessionsStatus(BaseTool):
    """Get session metadata (title, message count, tokens used, vibe)."""

    parallel_safe = True

    def __init__(self, db: SessionDB | None = None) -> None:
        self._db = db if db is not None else _resolve_default_db()

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SessionsStatus",
            description=(
                "Get metadata for a specific session by id: title, platform, "
                "model, message_count, input_tokens, output_tokens, vibe, "
                "created_at, last_active_at. Returns is_error=True with a clear "
                "message when the session id is not found in this profile's "
                "SessionDB. Use to check session existence or quick stats "
                "without loading the full message history."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session id whose row to look up.",
                    },
                },
                "required": ["session_id"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        sid = call.arguments["session_id"]
        info = self._db.get_session(sid)
        if info is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"unknown session {sid}",
                is_error=True,
            )
        return ToolResult(tool_call_id=call.id, content=str(info), is_error=False)


__all__ = ["SessionsHistory", "SessionsList", "SessionsStatus"]
