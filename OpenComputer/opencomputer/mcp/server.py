"""OpenComputer MCP server — exposes session history to MCP clients.

Lets external MCP clients (Claude Code, Cursor, …) query OC's sessions
and consent audit chain over stdio. Run via ``opencomputer mcp serve``.

Bridges OC ↔ Claude Code: when Saksham is coding in Claude Code and
wants to reference a discussion that happened in OC chat (Telegram /
CLI / cron output), Claude Code can call ``sessions_list`` /
``session_get`` / ``messages_read`` to surface it.

Read-only tools currently exposed (Tier-A item 14 expanded the surface
from 5 → 7):

- ``sessions_list(limit=20)`` — list recent sessions.
- ``session_get(session_id)`` — get one session's metadata.
- ``messages_read(session_id, limit=100)`` — read messages from a session.
- ``recall_search(query, limit=20)`` — FTS5 search across session history.
- ``consent_history(capability=None, limit=50)`` — F1 audit-log entries.
- ``channels_list()`` — distinct platforms with active sessions  (NEW)
- ``events_poll(since_message_id=0, limit=50)`` — incremental poll for
  new messages across all sessions (NEW)

Honest deferral — three Hermes write tools are NOT yet ported:

- ``messages_send`` — requires IPC with the gateway daemon (which holds
  the live channel adapter connections to Telegram / Discord / etc.).
  Implementing properly needs a SQLite ``pending_outgoing`` queue that
  the gateway consumes; in-progress design.
- ``permissions_respond`` — same IPC requirement against the F1
  pending-consent queue.
- ``events_wait`` — long-poll. The current MCP wire protocol returns one
  response per call, so true server-push isn't supported until the MCP
  spec evolves; clients can still ``events_poll`` on a timer.

Pattern: high-level ``mcp.server.fastmcp.FastMCP`` decorators (clean +
type-checked) over ``mcp.server.stdio.stdio_server()`` transport
(matches Claude Code MCP spec).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from mcp.server.fastmcp import FastMCP

from opencomputer.agent.config import _home
from opencomputer.agent.state import SessionDB

logger = logging.getLogger("opencomputer.mcp.server")


def build_server() -> FastMCP:
    """Construct the OpenComputer MCP server with all tools registered.

    The server is constructed each time so it picks up the active profile
    via ``_home()`` — ``opencomputer -p <profile> mcp serve`` works as
    expected.
    """
    server = FastMCP(
        name="opencomputer",
        instructions=(
            "OpenComputer session history bridge. Use these tools to query "
            "OC's session DB + F1 consent audit log. Useful when you want "
            "to reference past conversations from Telegram / CLI / cron "
            "while working in another agent."
        ),
    )

    @server.tool()
    def sessions_list(limit: int = 20) -> list[dict[str, Any]]:
        """List recent OpenComputer sessions across all platforms.

        Args:
            limit: Maximum number of sessions to return (default 20, max 200).

        Returns:
            List of session metadata dicts with id, started_at, platform,
            model, title, ended_at.
        """
        bounded = max(1, min(limit, 200))
        db = SessionDB(_home() / "sessions.db")
        return db.list_sessions(limit=bounded)

    @server.tool()
    def session_get(session_id: str) -> dict[str, Any] | None:
        """Get one session's metadata by id.

        Args:
            session_id: The session id (UUID hex string).

        Returns:
            Session metadata dict or ``None`` if not found.
        """
        db = SessionDB(_home() / "sessions.db")
        return db.get_session(session_id)

    @server.tool()
    def messages_read(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Read messages from a single OC session.

        Args:
            session_id: Which session to read.
            limit: Maximum number of messages to return (default 100).

        Returns:
            List of message dicts: role, content, tool_calls (if any),
            tool_call_id (for tool results), timestamp (if available).
        """
        bounded = max(1, min(limit, 1000))
        db = SessionDB(_home() / "sessions.db")
        messages = db.get_messages(session_id)
        out: list[dict[str, Any]] = []
        for msg in messages[:bounded]:
            entry: dict[str, Any] = {
                "role": msg.role,
                "content": msg.content,
            }
            if getattr(msg, "tool_calls", None):
                entry["tool_calls"] = [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in msg.tool_calls
                ]
            if getattr(msg, "tool_call_id", None):
                entry["tool_call_id"] = msg.tool_call_id
            out.append(entry)
        return out

    @server.tool()
    def recall_search(query: str, limit: int = 20) -> list[dict[str, Any]]:
        """FTS5 full-text search across all OC session messages.

        Args:
            query: Search expression (FTS5 syntax — e.g. ``"GUJALKALI breakout"``).
            limit: Max results to return.

        Returns:
            List of search hits with session_id, role, snippet, timestamp.
        """
        bounded = max(1, min(limit, 200))
        db = SessionDB(_home() / "sessions.db")
        return db.search(query, limit=bounded)

    @server.tool()
    def channels_list() -> list[dict[str, Any]]:
        """List distinct platforms with at least one OpenComputer session.

        Useful for an external MCP client to discover where OC is reachable —
        e.g. before a future ``messages_send`` it would consult this to
        learn the available platform values.

        Returns:
            One dict per platform with keys ``platform`` (e.g. "telegram"),
            ``session_count`` (how many distinct sessions exist on that
            platform), ``last_seen`` (ISO timestamp of the most recent
            session's start). Sorted by ``session_count`` descending so
            the user's most-active platforms surface first.
        """
        db_path = _home() / "sessions.db"
        if not db_path.exists():
            return []
        # Direct SQL — SessionDB doesn't expose a "platforms" helper and
        # adding one for one MCP tool isn't worth it.
        import datetime as _dt

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT platform, COUNT(*) as session_count, "
                    "MAX(started_at) as last_seen "
                    "FROM sessions WHERE platform IS NOT NULL "
                    "GROUP BY platform "
                    "ORDER BY session_count DESC"
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        out: list[dict[str, Any]] = []
        for r in rows:
            ts = r["last_seen"]
            iso = (
                _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).isoformat()
                if isinstance(ts, (int, float))
                else None
            )
            out.append(
                {
                    "platform": r["platform"],
                    "session_count": r["session_count"],
                    "last_seen": iso,
                }
            )
        return out

    @server.tool()
    def events_poll(
        since_message_id: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """Incremental poll for messages that arrived after a cursor.

        External MCP clients (Claude Code, Cursor) call this on a timer
        to pick up newly-received Telegram/Discord messages without
        having to enumerate every session. Returns a single batch plus
        the next cursor; clients should re-poll with ``next_cursor`` to
        continue the stream.

        Args:
            since_message_id: Cursor — return only messages whose row id
                is strictly greater than this. Use ``0`` on first call;
                use the returned ``next_cursor`` thereafter.
            limit: Max messages to return per call (default 50, max 500).

        Returns:
            Dict with ``messages`` (list of newest-N rows from the
            messages table joined with their session's platform / chat
            id) and ``next_cursor`` (the highest row id returned, or
            ``since_message_id`` if no new rows). Re-poll with that
            cursor to get the next slice.
        """
        bounded = max(1, min(limit, 500))
        db_path = _home() / "sessions.db"
        if not db_path.exists():
            return {"messages": [], "next_cursor": since_message_id}

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                # Join messages → sessions so the caller learns the
                # platform without a follow-up ``session_get`` per row.
                # Chat id is encoded into ``session_id`` by the gateway —
                # callers that need a structured (platform, chat_id)
                # tuple should call ``session_get`` for resolution.
                rows = conn.execute(
                    "SELECT m.id, m.session_id, m.role, m.content, "
                    "m.timestamp, s.platform "
                    "FROM messages m "
                    "JOIN sessions s ON m.session_id = s.id "
                    "WHERE m.id > ? "
                    "ORDER BY m.id ASC LIMIT ?",
                    (since_message_id, bounded),
                ).fetchall()
            except sqlite3.OperationalError:
                # Pre-migration DB or missing column; return empty.
                return {"messages": [], "next_cursor": since_message_id}

        messages = [dict(r) for r in rows]
        next_cursor = messages[-1]["id"] if messages else since_message_id
        return {"messages": messages, "next_cursor": next_cursor}

    @server.tool()
    def consent_history(capability: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Read F1 consent audit-log entries (HMAC-chained tamper-evident).

        Args:
            capability: Optional filter — only entries for this capability_id
                (e.g. ``"cron.create"``, ``"oi_bridge.screenshot"``).
            limit: Max entries to return.

        Returns:
            List of audit entries with id, timestamp, capability_id, action
            (granted/revoked/auto), tier, scope, granted_by.
        """
        bounded = max(1, min(limit, 500))
        db_path = _home() / "sessions.db"
        if not db_path.exists():
            return []

        sql = "SELECT id, ts, capability_id, action, tier, scope, granted_by FROM audit_log"
        params: list[Any] = []
        if capability:
            sql += " WHERE capability_id = ?"
            params.append(capability)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(bounded)

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                # F1 audit_log table not present — likely a pre-F1 session DB
                # or a fresh profile. Return empty rather than raise.
                return []
            return [dict(r) for r in rows]

    return server


async def run_server() -> None:
    """Start the MCP server on stdio and run until the client disconnects.

    Used by ``opencomputer mcp serve``. Blocks until stdio closes.
    """
    server = build_server()
    logger.info("opencomputer MCP server starting on stdio")
    # FastMCP's stdio runner handles the async transport lifecycle.
    await server.run_stdio_async()


def main() -> None:
    """Synchronous entry point for ``opencomputer mcp serve``."""
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("opencomputer MCP server stopped (KeyboardInterrupt)")


__all__ = ["build_server", "main", "run_server"]
