"""OpenComputer MCP server — exposes session history to MCP clients.

Lets external MCP clients (Claude Code, Cursor, …) query OC's sessions
and consent audit chain over stdio. Run via ``opencomputer mcp serve``.

Bridges OC ↔ Claude Code: when Saksham is coding in Claude Code and
wants to reference a discussion that happened in OC chat (Telegram /
CLI / cron output), Claude Code can call ``sessions_list`` /
``session_get`` / ``messages_read`` to surface it.

Five tools exposed (G.6 / Tier 2.2 minimum slice):

- ``sessions_list(limit=20)`` — list recent sessions with id/platform/title.
- ``session_get(session_id)`` — get one session's metadata.
- ``messages_read(session_id, limit=100)`` — read messages from a session.
- ``recall_search(query, limit=20)`` — FTS5 search across session history.
- ``consent_history(capability=None, limit=50)`` — F1 audit-log entries.

Deviation from Hermes ``mcp_serve.py`` (10 tools): OC ships read-only first
because that's the bridges-OC-to-Claude-Code use case; the bidirectional
slice (``messages_send`` / ``events_poll`` / ``events_wait`` /
``permissions_respond`` / ``attachments_fetch`` / ``channels_list``) is
deferred to Phase 12m / a G.6.x follow-up so we don't ship a
write-capable MCP surface before F1 consent has been live for a while.

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
    def consent_history(capability: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Read F1 consent audit-log entries (HMAC-chained tamper-evident).

        Args:
            capability: Optional filter — only entries for this capability_id
                (e.g. ``"cron.create"``, ``"opencli_scraper.scrape_raw"``).
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
