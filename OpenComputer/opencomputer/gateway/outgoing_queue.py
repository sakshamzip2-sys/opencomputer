"""SQLite-backed outgoing-message queue.

Bridges processes that need to send messages to platform adapters but
don't own the live adapter connection — primarily ``opencomputer mcp
serve`` (a separate stdio process) reaching out to Telegram/Discord
through the gateway daemon's adapters.

Flow:

::

  MCP client (Claude Code)
       │  messages_send(platform, chat_id, body)
       ▼
  opencomputer mcp serve
       │  OutgoingQueue.enqueue()
       ▼
  SQLite outgoing_messages table  ◄────┐
       │ polled                        │
       ▼ (every 1s)                    │
  Gateway daemon                       │
       │  adapter.send()               │
       ▼ on success: mark_sent ─────────┘

Failure modes:

- **Gateway not running** — message stays ``queued`` indefinitely. When
  the gateway boots, it drains. Acceptable: user explicitly chose to
  use a no-daemon CLI mode and the alternative would be silently
  dropping the send.
- **Adapter rejects (auth fail, chat not found)** — marked ``failed``
  with the error string. User sees via ``opencomputer outgoing list``.
- **Per-row TTL** — rows older than 7 days that never sent get marked
  ``expired`` on next gateway boot. Prevents indefinite buildup if the
  user paired a platform once + later removed credentials.

Schema is added with ``CREATE TABLE IF NOT EXISTS`` (no ``SCHEMA_VERSION``
bump) so this composes with concurrent migrations from other PRs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("opencomputer.gateway.outgoing_queue")


SendStatus = Literal["queued", "sent", "failed", "expired"]


_DDL = """
CREATE TABLE IF NOT EXISTS outgoing_messages (
    id            TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    body          TEXT NOT NULL,
    attachments   TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',
    error         TEXT,
    enqueued_at   REAL NOT NULL,
    sent_at       REAL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    metadata      TEXT
);

CREATE INDEX IF NOT EXISTS idx_outgoing_status ON outgoing_messages(status, enqueued_at);
"""

#: Drop messages that never delivered after this many seconds. 7 days.
_EXPIRY_SECONDS = 7 * 86400


@dataclass
class OutgoingMessage:
    id: str
    platform: str
    chat_id: str
    body: str
    status: SendStatus
    enqueued_at: float
    attachments: list[str] = field(default_factory=list)
    error: str | None = None
    sent_at: float | None = None
    attempts: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> OutgoingMessage:
        atts_raw = row["attachments"]
        meta_raw = row["metadata"]
        return cls(
            id=row["id"],
            platform=row["platform"],
            chat_id=row["chat_id"],
            body=row["body"],
            attachments=json.loads(atts_raw) if atts_raw else [],
            status=row["status"],
            error=row["error"],
            enqueued_at=row["enqueued_at"],
            sent_at=row["sent_at"],
            attempts=int(row["attempts"] or 0),
            metadata=json.loads(meta_raw) if meta_raw else {},
        )


class OutgoingQueue:
    """SQLite-backed CRUD for the outgoing-message queue."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_DDL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        finally:
            conn.close()

    # ─── enqueue ──────────────────────────────────────────────────

    def enqueue(
        self,
        *,
        platform: str,
        chat_id: str,
        body: str,
        attachments: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OutgoingMessage:
        msg_id = uuid.uuid4().hex[:12]
        now = time.time()
        atts_json = json.dumps(attachments or [])
        meta_json = json.dumps(metadata or {})
        with self._txn() as conn:
            conn.execute(
                "INSERT INTO outgoing_messages "
                "(id, platform, chat_id, body, attachments, status, "
                "enqueued_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
                (msg_id, platform, chat_id, body, atts_json, now, meta_json),
            )
        return OutgoingMessage(
            id=msg_id,
            platform=platform,
            chat_id=chat_id,
            body=body,
            attachments=attachments or [],
            status="queued",
            enqueued_at=now,
            metadata=metadata or {},
        )

    # ─── dequeue / drain ──────────────────────────────────────────

    def list_queued(self, limit: int = 16) -> list[OutgoingMessage]:
        """Oldest-first FIFO drain order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM outgoing_messages WHERE status = 'queued' "
                "ORDER BY enqueued_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [OutgoingMessage.from_row(r) for r in rows]

    def list_(
        self, *, status: SendStatus | None = None, limit: int = 100
    ) -> list[OutgoingMessage]:
        sql = "SELECT * FROM outgoing_messages"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY enqueued_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [OutgoingMessage.from_row(r) for r in rows]

    def get(self, msg_id: str) -> OutgoingMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM outgoing_messages WHERE id = ?", (msg_id,),
            ).fetchone()
        return OutgoingMessage.from_row(row) if row else None

    # ─── status updates ──────────────────────────────────────────

    def mark_sent(self, msg_id: str) -> None:
        now = time.time()
        with self._txn() as conn:
            conn.execute(
                "UPDATE outgoing_messages SET status='sent', sent_at=?, "
                "attempts=attempts+1 WHERE id=?",
                (now, msg_id),
            )

    def mark_failed(self, msg_id: str, error: str) -> None:
        with self._txn() as conn:
            conn.execute(
                "UPDATE outgoing_messages SET status='failed', error=?, "
                "attempts=attempts+1 WHERE id=?",
                (error, msg_id),
            )

    def expire_stale(self) -> int:
        """Mark queued rows older than ``_EXPIRY_SECONDS`` as expired.

        Called once at gateway start so rows enqueued during a removed-
        credentials period don't sit forever.
        """
        cutoff = time.time() - _EXPIRY_SECONDS
        with self._txn() as conn:
            cur = conn.execute(
                "UPDATE outgoing_messages SET status='expired' "
                "WHERE status='queued' AND enqueued_at < ?",
                (cutoff,),
            )
            return int(cur.rowcount)


__all__ = ["OutgoingMessage", "OutgoingQueue", "SendStatus"]
