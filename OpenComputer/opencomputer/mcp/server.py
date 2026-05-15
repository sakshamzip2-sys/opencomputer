"""OpenComputer MCP server — exposes OC's session history + send-out path to MCP clients.

Lets external MCP clients (Claude Code, Cursor, …) query OC's sessions,
long-poll for new messages, and send messages back out through OC's
live channel adapters — all over stdio. Run via ``opencomputer mcp
serve``.

The bidirectional bridge: Claude Code reading + replying on Telegram
via OC, without Saksham having to context-switch between the two.

Read-only tools (7):

- ``sessions_list(limit=20)`` — list recent sessions.
- ``session_get(session_id)`` — get one session's metadata.
- ``messages_read(session_id, limit=100)`` — read messages from a session.
- ``recall_search(query, limit=20)`` — FTS5 search across session history.
- ``consent_history(capability=None, limit=50)`` — F1 audit-log entries.
- ``channels_list()`` — distinct platforms with active sessions.
- ``events_poll(since_message_id=0, limit=50)`` — incremental cursor poll.

Write tools + long-poll (3, all Tier-A item 14 follow-up):

- ``messages_send(platform, chat_id, body)`` — enqueue outbound; the
  gateway daemon picks it up within ~1s and dispatches via the live
  adapter for ``platform``. Returns immediately with a queue id.
- ``messages_send_status(message_id)`` — look up delivery state of a
  previously-queued send.
- ``events_wait(since_message_id, timeout_s)`` — long-poll wrapper
  around ``events_poll`` that blocks until a new message arrives or
  timeout (capped at 120s).

F1 consent write-back (1):

- ``permissions_respond(capability_id, decision, scope=None,
  tier=1, expires_in_seconds=None)`` — grant or revoke a capability.
  ``decision="allow"`` upserts a ConsentGrant; ``decision="deny"`` revokes
  the matching grant. Honors F1's HMAC-chained audit trail: the next
  ``consent_history`` call surfaces the new entry.

Pattern: high-level ``mcp.server.fastmcp.FastMCP`` decorators (clean +
type-checked) over ``mcp.server.stdio.stdio_server()`` transport
(matches Claude Code MCP spec).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from opencomputer.agent.config import _home
from opencomputer.agent.state import SessionDB

logger = logging.getLogger("opencomputer.mcp.server")


#: Allowlist for ``permissions_respond``'s ``granted_by`` arg (M3).
#: Wider than just "user" so MCP-driven grants and gateway-driven grants
#: surface distinctly in the audit log without schema migration. New
#: sources must be added here intentionally so a misconfigured caller
#: can't synthesize arbitrary attribution.
_VALID_GRANTED_BY: frozenset[str] = frozenset({"user", "mcp_client", "gateway"})


def build_server(enable_approvals: bool = False) -> FastMCP:
    """Construct the OpenComputer MCP server with all tools registered.

    The server is constructed each time so it picks up the active profile
    via ``_home()`` — ``opencomputer -p <profile> mcp serve`` works as
    expected.

    mcp-openclaw-port M3 (2026-05-15) — when ``enable_approvals=True``,
    register the long-poll ``permissions_request_subscribe`` tool so
    external MCP clients (Claude Code, Cursor, IDE plugins) can drive
    OC's consent prompt queue. Default ``False`` (security-conservative):
    consent state is sensitive and must be opt-in to expose remotely.
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

    # G12 (Hermes parity, 2026-05-09): spec-named aliases. Same body,
    # second tool name. FastMCP keys the registry on tool name so
    # registering ``conversations_list`` next to ``sessions_list`` adds
    # a second registration that delegates to the same body — no
    # collision because each is a unique tool name.
    @server.tool()
    def conversations_list(limit: int = 20) -> list[dict[str, Any]]:
        """Hermes-spec alias for ``sessions_list`` (G12 — 2026-05-09)."""
        bounded = max(1, min(limit, 200))
        db = SessionDB(_home() / "sessions.db")
        return db.list_sessions(limit=bounded)

    @server.tool()
    def conversation_get(session_id: str) -> dict[str, Any] | None:
        """Hermes-spec alias for ``session_get`` (G12 — 2026-05-09)."""
        db = SessionDB(_home() / "sessions.db")
        return db.get_session(session_id)

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
    def attachments_fetch(session_id: str, message_id: int) -> list[dict[str, Any]]:
        """Fetch attachment file contents for a specific message.

        Reads the ``attachments`` JSON column (added 2026-04-27) from the
        messages table and returns base64-encoded content for each path
        that still exists on disk. Useful for MCP clients that want to
        inspect files the user attached to a prior conversation.

        Args:
            session_id: The session that owns the message.
            message_id: Row id of the message whose attachments to fetch.

        Returns:
            List of dicts with ``path`` (original file path), ``mime_type``
            (guessed from extension, or ``application/octet-stream``), and
            ``content_b64`` (base64-encoded file bytes). Entries for files
            that no longer exist on disk are silently skipped.
        """
        import base64
        import json as _json
        import mimetypes
        import pathlib

        db_path = _home() / "sessions.db"
        try:
            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute(
                    "SELECT attachments FROM messages WHERE id=? AND session_id=?",
                    (message_id, session_id),
                ).fetchone()
        except sqlite3.Error:
            return []
        if not row or not row[0]:
            return []
        try:
            paths: list[str] = _json.loads(row[0])
        except (ValueError, TypeError):
            return []
        result: list[dict[str, Any]] = []
        for path in paths:
            try:
                p = pathlib.Path(path)
                if not p.exists():
                    continue
                raw = p.read_bytes()
                mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
                result.append({
                    "path": path,
                    "mime_type": mime,
                    "content_b64": base64.b64encode(raw).decode(),
                })
            except OSError:
                continue
        return result

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
        since_message_id: int = 0,
        after_cursor: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Incremental poll for messages and approval events.

        External MCP clients (Claude Code, Cursor) call this on a timer
        to pick up newly-received Telegram/Discord messages without
        having to enumerate every session. Returns a single batch plus
        the next cursor; clients should re-poll with ``next_cursor`` to
        continue the stream.

        Hermes parity G14 (2026-05-09):

        * Accepts ``after_cursor`` as a Hermes-spec alias for
          ``since_message_id`` (when both are supplied, ``after_cursor``
          wins).
        * Surfaces F1 ``audit_log`` entries newer than the cursor under
          a separate ``approvals`` key with ``type``
          (``approval_requested`` / ``approval_resolved``) so MCP
          clients can react to consent grants/revocations elsewhere.
          Empty when the ``audit_log`` table is absent.

        Args:
            since_message_id: Cursor (legacy OC). Use ``after_cursor``
                instead for new code.
            after_cursor: Hermes-spec cursor name. Wins when both are set.
            limit: Max messages and approvals returned (default 50, max 500).

        Returns:
            Dict with ``messages`` (list of newest-N message rows joined
            with their session's platform), ``next_cursor`` (the highest
            message id returned), and ``approvals`` (list of newer-than-
            cursor audit_log entries with ``type`` set).
        """
        cursor = after_cursor if after_cursor is not None else since_message_id
        bounded = max(1, min(limit, 500))
        db_path = _home() / "sessions.db"
        if not db_path.exists():
            return {"messages": [], "next_cursor": cursor, "approvals": []}

        out_messages: list[dict[str, Any]] = []
        out_approvals: list[dict[str, Any]] = []
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT m.id, m.session_id, m.role, m.content, "
                    "m.timestamp, s.platform "
                    "FROM messages m "
                    "JOIN sessions s ON m.session_id = s.id "
                    "WHERE m.id > ? "
                    "ORDER BY m.id ASC LIMIT ?",
                    (cursor, bounded),
                ).fetchall()
            except sqlite3.OperationalError:
                # Pre-migration DB or missing column; return empty.
                rows = []
            out_messages = [dict(r) for r in rows]

            # G14: surface approval events from audit_log when present.
            try:
                a_rows = conn.execute(
                    "SELECT id, ts, capability_id, action, tier, scope, "
                    "granted_by FROM audit_log WHERE id > ? "
                    "ORDER BY id ASC LIMIT ?",
                    (cursor, bounded),
                ).fetchall()
                for r in a_rows:
                    rd = dict(r)
                    rd["type"] = (
                        "approval_resolved"
                        if rd["action"] in ("granted", "revoked")
                        else "approval_requested"
                    )
                    out_approvals.append(rd)
            except sqlite3.OperationalError:
                # Pre-F1 profile or fresh DB — silently skip.
                pass

        next_cursor = out_messages[-1]["id"] if out_messages else cursor
        return {
            "messages": out_messages,
            "next_cursor": next_cursor,
            "approvals": out_approvals,
        }

    @server.tool()
    def consent_history(capability: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Read F1 consent audit-log entries (HMAC-chained tamper-evident).

        Args:
            capability: Optional filter — only entries for this capability_id
                (e.g. ``"cron.create"``, ``"introspection.screenshot"``).
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

    @server.tool()
    def permissions_list_open(limit: int = 50) -> list[dict[str, Any]]:
        """Hermes parity G13 (2026-05-09): list OPEN consent requests.

        Returns capabilities currently awaiting a user/operator decision.
        Distinct from ``consent_history`` (which returns the full audit
        log) — this is the live "approvals queue".

        Falls back to ``[]`` if the F1 ``consent_requests`` table doesn't
        exist (pre-F1 profile or fresh DB).

        Args:
            limit: Max entries to return (default 50, max 500).

        Returns:
            List of dicts: ``capability_id`` (e.g. "fs.write"), ``scope``
            (path / arg constraints), ``requested_at`` (unix-ts float),
            ``requested_by`` (caller — usually ``"tool:<name>"``).
        """
        bounded = max(1, min(limit, 500))
        db_path = _home() / "sessions.db"
        if not db_path.exists():
            return []
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT capability_id, scope, requested_at, requested_by "
                    "FROM consent_requests WHERE state = 'pending' "
                    "ORDER BY requested_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    @server.tool()
    async def messages_send(
        platform: str,
        chat_id: str,
        body: str,
        thread_hint: str | None = None,
    ) -> dict[str, Any]:
        """Send a message via the gateway daemon's channel adapter.

        Writes to OC's outgoing-message queue; the gateway daemon picks
        it up within ~1s and dispatches via the live adapter for
        ``platform``. Returns immediately with the queue id; the caller
        should not assume delivery synchronously.

        Args:
            platform: One of ``"telegram"``, ``"discord"``, ``"slack"``,
                etc. Must match a platform with a paired adapter on the
                gateway. Use ``channels_list()`` to discover the active
                set.
            chat_id: Platform-native chat / channel / DM identifier
                (string form). For Telegram this is the chat id (e.g.
                ``"123456789"``); for Discord it's the channel id.
            body: Message text. Plaintext only — adapter-side rendering
                handles platform-native formatting.
            thread_hint: Item 21 — optional topic tag. Replies that
                preserve this hint (or the same incoming-message context
                in a future PR) will derive a SEPARATE OpenComputer
                session from the same chat. Use cases: cron output
                (``thread_hint="cron:morning-briefing"``) so its
                follow-ups don't pollute the ad-hoc Q&A thread; a fresh
                topic kicked off by an external script. Default
                ``None`` reproduces the legacy "same chat = same session"
                behaviour.

        Returns:
            Dict with ``id`` (queue row id), ``status`` (always
            ``"queued"`` here — the queue id is the source of truth;
            check ``messages_send_status`` for delivery state), and
            ``note`` (one-line user-facing hint about what to do next).

        Notes:
            - If no gateway daemon is running, the row stays ``queued``
              indefinitely. Either start the gateway with
              ``opencomputer gateway`` or use the ``opencomputer
              outgoing list`` CLI to inspect.
            - Failures (auth, chat not found) get marked ``failed`` on
              the queue row with the error text — fetch via
              ``messages_send_status``.
        """
        from opencomputer.gateway.outgoing_queue import OutgoingQueue

        queue = OutgoingQueue(_home() / "sessions.db")
        metadata: dict[str, Any] = {}
        if thread_hint:
            metadata["thread_hint"] = thread_hint
        msg = queue.enqueue(
            platform=platform, chat_id=chat_id, body=body, metadata=metadata,
        )
        return {
            "id": msg.id,
            "status": msg.status,
            "thread_hint": thread_hint,
            "note": (
                "Queued for delivery. The gateway daemon drains the queue "
                "every second; if no gateway is running the message waits."
            ),
        }

    @server.tool()
    def messages_send_status(message_id: str) -> dict[str, Any] | None:
        """Look up the delivery state of a previously-queued send.

        Args:
            message_id: The id returned by ``messages_send``.

        Returns:
            Dict with ``id``, ``platform``, ``chat_id``, ``status``
            (``queued | sent | failed | expired``), ``error`` (if
            failed), ``enqueued_at``, ``sent_at``, ``attempts``. Or
            ``None`` if no such id exists.
        """
        from opencomputer.gateway.outgoing_queue import OutgoingQueue

        queue = OutgoingQueue(_home() / "sessions.db")
        msg = queue.get(message_id)
        if msg is None:
            return None
        return {
            "id": msg.id,
            "platform": msg.platform,
            "chat_id": msg.chat_id,
            "body": msg.body,
            "status": msg.status,
            "error": msg.error,
            "enqueued_at": msg.enqueued_at,
            "sent_at": msg.sent_at,
            "attempts": msg.attempts,
        }

    @server.tool()
    async def events_wait(
        since_message_id: int = 0,
        timeout_s: float = 30.0,
        poll_interval_s: float = 1.0,
    ) -> dict[str, Any]:
        """Long-poll for new messages — block until at least one arrives or timeout.

        Wraps ``events_poll`` with a sleep loop. Returns the same shape
        as ``events_poll``. Use this from MCP clients that want
        real-time-ish notifications without a busy poll.

        Args:
            since_message_id: Cursor — return messages with id > this.
                Same semantics as ``events_poll``.
            timeout_s: Max seconds to wait. Default 30. Caps at 120 to
                avoid pathological MCP timeouts; clients wanting longer
                holds should call again.
            poll_interval_s: How often to check (default 1s).

        Returns:
            Same shape as ``events_poll`` — ``{messages, next_cursor}``.
            On timeout returns ``{messages: [], next_cursor:
            since_message_id}``.
        """
        bounded_timeout = max(0.5, min(timeout_s, 120.0))
        bounded_poll = max(0.1, min(poll_interval_s, 5.0))
        deadline = asyncio.get_event_loop().time() + bounded_timeout
        cursor = since_message_id
        while True:
            # Inline the same query as events_poll — duplicating the SQL
            # is cheaper than restructuring events_poll into a callable.
            db_path = _home() / "sessions.db"
            if db_path.exists():
                with sqlite3.connect(str(db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    try:
                        rows = conn.execute(
                            "SELECT m.id, m.session_id, m.role, m.content, "
                            "m.timestamp, s.platform "
                            "FROM messages m "
                            "JOIN sessions s ON m.session_id = s.id "
                            "WHERE m.id > ? "
                            "ORDER BY m.id ASC LIMIT 50",
                            (cursor,),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                if rows:
                    msgs = [dict(r) for r in rows]
                    return {"messages": msgs, "next_cursor": msgs[-1]["id"]}
            # No new messages — sleep, retry until deadline.
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                return {"messages": [], "next_cursor": cursor}
            await asyncio.sleep(min(bounded_poll, max(0.0, deadline - now)))

    @server.tool()
    def permissions_respond(
        capability_id: str,
        decision: str,
        scope: str | None = None,
        tier: int = 1,
        expires_in_seconds: int | None = None,
        granted_by: str = "user",
    ) -> dict[str, Any]:
        """Grant or revoke an F1 capability consent (10th of 10 Hermes tools).

        Args:
            capability_id: The capability id (e.g. "fs.read", "shell.exec").
            decision: ``"allow"`` (upsert grant) or ``"deny"`` (revoke).
            scope: Optional scope filter (e.g. a path prefix). ``None``
                means a global grant for the capability.
            tier: ConsentTier int (0=IMPLICIT, 1=EXPLICIT (default),
                2=PER_ACTION, 3=DELEGATED).
            expires_in_seconds: Optional grant lifetime. ``None`` =
                no expiry (revocable). Otherwise added to ``time.time()``.
            granted_by: M3 (mcp-openclaw-port). Source attribution
                recorded in the consent_grants table. Must be one of
                ``"user"`` (CLI/TUI driver), ``"mcp_client"`` (external
                MCP client over this server's stdio surface), or
                ``"gateway"`` (channel adapter). Defaults to ``"user"``
                so back-compat call sites keep working.

        Returns:
            ``{"ok": True, "action": "granted"|"revoked", "capability_id":
            ..., "scope": ...}`` on success;
            ``{"ok": False, "error": "..."}`` on bad input.
        """
        import time as _time

        from opencomputer.agent.consent.store import ConsentStore
        from plugin_sdk.consent import ConsentGrant, ConsentTier

        decision_norm = (decision or "").strip().lower()
        if decision_norm not in ("allow", "deny"):
            return {
                "ok": False,
                "error": f"decision must be 'allow' or 'deny', got {decision!r}",
            }
        # M3 — granted_by attribution validation. Reject unknown values
        # so a buggy caller can't synthesize forged audit-source labels.
        granted_by_norm = (granted_by or "user").strip()
        if granted_by_norm not in _VALID_GRANTED_BY:
            return {
                "ok": False,
                "error": (
                    f"granted_by must be one of {sorted(_VALID_GRANTED_BY)}, "
                    f"got {granted_by!r}"
                ),
            }
        try:
            tier_enum = ConsentTier(int(tier))
        except (TypeError, ValueError):
            return {"ok": False, "error": f"tier must be 0..3, got {tier!r}"}

        db_path = _home() / "sessions.db"
        if not db_path.exists():
            return {"ok": False, "error": "sessions.db not found for active profile"}
        try:
            with sqlite3.connect(str(db_path)) as conn:
                store = ConsentStore(conn)
                if decision_norm == "deny":
                    store.revoke(capability_id, scope)
                    action = "revoked"
                else:
                    now = _time.time()
                    expires_at: float | None = None
                    if expires_in_seconds is not None and expires_in_seconds > 0:
                        expires_at = now + float(expires_in_seconds)
                    grant = ConsentGrant(
                        capability_id=capability_id,
                        scope_filter=scope,
                        tier=tier_enum,
                        granted_at=now,
                        expires_at=expires_at,
                        granted_by=granted_by_norm,
                    )
                    store.upsert(grant)
                    action = "granted"
            return {
                "ok": True,
                "action": action,
                "capability_id": capability_id,
                "scope": scope,
                "tier": int(tier_enum),
                "granted_by": granted_by_norm,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ── M3 (mcp-openclaw-port): permission long-poll subscription ──
    # Opt-in via ``enable_approvals`` — external MCP clients can drive
    # OC's consent flow. Default OFF (consent state is sensitive; must
    # be intentionally exposed via ``oc mcp serve --enable-approvals``).
    if enable_approvals:

        @server.tool()
        async def permissions_request_subscribe(
            ctx: Context,
            timeout_s: float = 30.0,
            poll_interval_s: float = 1.0,
        ) -> list[dict[str, Any]]:
            """Long-poll for pending F1 consent requests (M3 + Gap H push).

            Wraps the same query as :func:`permissions_list_open` in a
            sleep loop so external MCP clients (Claude Code, Cursor,
            IDE plugins) can block on permission events instead of
            busy-polling.

            Gap H push semantics (mcp-openclaw-port follow-up): when a
            new pending request is detected during the poll, the server
            ALSO emits a ``notifications/message`` (LoggingMessageNotification)
            with ``logger="openclaw.permission"`` and the request data
            in the payload. Clients that listen for MCP log
            notifications get out-of-band push UX even while another
            tool call is in flight.

            Args:
                timeout_s: Max seconds to wait. Default 30, capped at
                    120 to avoid pathological MCP timeouts; clients
                    wanting longer holds should call again.
                poll_interval_s: How often to check (default 1s).
                ctx: FastMCP Context auto-injected by the SDK when
                    available. Used for the push-notification path.

            Returns:
                List of pending consent-request dicts (capability_id,
                scope, requested_at, requested_by). Empty list on
                timeout with no entries.
            """
            bounded_timeout = max(0.05, min(timeout_s, 120.0))
            bounded_poll = max(0.05, min(poll_interval_s, 5.0))
            deadline = asyncio.get_event_loop().time() + bounded_timeout
            db_path = _home() / "sessions.db"
            while True:
                if db_path.exists():
                    try:
                        with sqlite3.connect(str(db_path)) as conn:
                            conn.row_factory = sqlite3.Row
                            rows = conn.execute(
                                "SELECT capability_id, scope, requested_at, "
                                "requested_by "
                                "FROM consent_requests WHERE state = 'pending' "
                                "ORDER BY requested_at DESC LIMIT 50"
                            ).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                    if rows:
                        # Gap H — emit push notification BEFORE returning
                        # so the client receives the event out-of-band
                        # via its log-message subscription (in addition
                        # to the long-poll return value below). Best-
                        # effort: any transport error here is swallowed
                        # so the long-poll response still returns.
                        try:
                            for row in rows:
                                await ctx.session.send_log_message(
                                    level="info",
                                    data={
                                        "event": "openclaw.permission.requested",
                                        "capability_id": row["capability_id"],
                                        "scope": row["scope"],
                                        "requested_at": row["requested_at"],
                                        "requested_by": row["requested_by"],
                                    },
                                    logger="openclaw.permission",
                                )
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "permission-requested push notification "
                                "failed; long-poll response unaffected",
                                exc_info=True,
                            )
                        return [dict(r) for r in rows]
                now = asyncio.get_event_loop().time()
                if now >= deadline:
                    return []
                await asyncio.sleep(min(bounded_poll, max(0.0, deadline - now)))

    return server


async def run_server(*, enable_approvals: bool = False) -> None:
    """Start the MCP server on stdio and run until the client disconnects.

    Used by ``opencomputer mcp serve``. Blocks until stdio closes.

    ``enable_approvals`` toggles the M3 long-poll
    ``permissions_request_subscribe`` tool. Default OFF for security
    (consent state is sensitive — only expose intentionally).
    """
    server = build_server(enable_approvals=enable_approvals)
    logger.info(
        "opencomputer MCP server starting on stdio (approvals=%s)",
        "ON" if enable_approvals else "OFF",
    )
    # FastMCP's stdio runner handles the async transport lifecycle.
    await server.run_stdio_async()


def main(*, enable_approvals: bool = False) -> None:
    """Synchronous entry point for ``opencomputer mcp serve``."""
    try:
        asyncio.run(run_server(enable_approvals=enable_approvals))
    except KeyboardInterrupt:
        logger.info("opencomputer MCP server stopped (KeyboardInterrupt)")


__all__ = ["build_server", "main", "run_server"]
