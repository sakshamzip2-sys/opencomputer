"""
Session state — SQLite with FTS5 full-text search.

Schema inspired by hermes-agent/hermes_state.py. Kept minimal:
- sessions: one row per conversation
- messages: one row per turn (role + content + tool_calls JSON)
- messages_fts: FTS5 virtual table for cross-session search

Uses WAL mode + application-level retry jitter for concurrency.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from plugin_sdk.core import Message, ToolCall

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    started_at    REAL NOT NULL,
    ended_at      REAL,
    platform      TEXT NOT NULL,
    model         TEXT,
    title         TEXT,
    message_count INTEGER DEFAULT 0,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    tool_call_id  TEXT,
    tool_calls    TEXT,   -- JSON array if role=assistant + tool calls
    name          TEXT,   -- tool name for role=tool
    reasoning     TEXT,   -- extended thinking
    timestamp     REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert
AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete
AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update
AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class SessionDB:
    """Lightweight SQLite wrapper for session storage + FTS5 search."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,  # autocommit; we manage transactions explicitly
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(DDL)
            cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
            row = cur.fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """Run a transaction with retry+jitter on SQLITE_BUSY (adapted from hermes)."""
        conn = self._connect()
        attempts = 0
        max_attempts = 5
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                attempts += 1
                if attempts >= max_attempts:
                    raise
                time.sleep(random.uniform(0.02, 0.15))
            finally:
                conn.close()

    # ─── sessions ─────────────────────────────────────────────────

    def create_session(
        self, session_id: str, platform: str = "cli", model: str = "", title: str = ""
    ) -> None:
        with self._txn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (id, started_at, platform, model, title) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, time.time(), platform, model, title),
            )

    def end_session(self, session_id: str) -> None:
        with self._txn() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (time.time(), session_id),
            )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ─── messages ─────────────────────────────────────────────────

    @staticmethod
    def _msg_row(session_id: str, msg: Message) -> tuple:
        tool_calls_json = (
            json.dumps(
                [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in msg.tool_calls
                ]
            )
            if msg.tool_calls
            else None
        )
        return (
            session_id,
            msg.role,
            msg.content,
            msg.tool_call_id,
            tool_calls_json,
            msg.name,
            msg.reasoning,
            time.time(),
        )

    def append_message(self, session_id: str, msg: Message) -> int:
        with self._txn() as conn:
            cur = conn.execute(
                "INSERT INTO messages "
                "(session_id, role, content, tool_call_id, tool_calls, name, reasoning, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                self._msg_row(session_id, msg),
            )
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (session_id,),
            )
            return int(cur.lastrowid or 0)

    def append_messages_batch(self, session_id: str, msgs: list[Message]) -> list[int]:
        """Insert multiple messages atomically in a single transaction.

        Used by the agent loop to persist an assistant message together with its
        tool_result messages so a cancellation between writes cannot leave the DB
        with a dangling tool_use that has no matching tool_result (which causes
        Anthropic to 400 on resume).
        """
        if not msgs:
            return []
        with self._txn() as conn:
            ids: list[int] = []
            for msg in msgs:
                cur = conn.execute(
                    "INSERT INTO messages "
                    "(session_id, role, content, tool_call_id, tool_calls, name, reasoning, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    self._msg_row(session_id, msg),
                )
                ids.append(int(cur.lastrowid or 0))
            conn.execute(
                "UPDATE sessions SET message_count = message_count + ? WHERE id = ?",
                (len(msgs), session_id),
            )
            return ids

    def get_messages(self, session_id: str) -> list[Message]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, tool_call_id, tool_calls, name, reasoning "
                "FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        out: list[Message] = []
        for r in rows:
            tool_calls = None
            if r["tool_calls"]:
                raw = json.loads(r["tool_calls"])
                tool_calls = [
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                    for tc in raw
                ]
            out.append(
                Message(
                    role=r["role"],
                    content=r["content"],
                    tool_call_id=r["tool_call_id"],
                    tool_calls=tool_calls,
                    name=r["name"],
                    reasoning=r["reasoning"],
                )
            )
        return out

    # ─── FTS5 search ──────────────────────────────────────────────

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search across all messages. Returns snippet + metadata."""
        # Simple sanitization — let FTS5 reject truly malformed queries
        safe_q = query.replace('"', '""').strip()
        if not safe_q:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.session_id, m.role, m.timestamp, "
                "snippet(messages_fts, 0, '[', ']', '…', 20) AS snippet "
                "FROM messages_fts "
                "JOIN messages m ON m.id = messages_fts.rowid "
                "WHERE messages_fts MATCH ? "
                "ORDER BY m.timestamp DESC LIMIT ?",
                (safe_q, limit),
            ).fetchall()
            return [dict(r) for r in rows]


__all__ = ["SessionDB"]
