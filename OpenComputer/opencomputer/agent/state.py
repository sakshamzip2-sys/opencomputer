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

#: Incremented when the SQLite schema is extended. Migration at open
#: time compares ``SELECT version FROM schema_version`` against this
#: constant and fires idempotent ``ALTER TABLE`` statements for columns
#: added since. Existing rows keep their data — new columns default to
#: NULL for older messages.
SCHEMA_VERSION = 2

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
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id             TEXT NOT NULL,
    role                   TEXT NOT NULL,
    content                TEXT NOT NULL,
    tool_call_id           TEXT,
    tool_calls             TEXT,   -- JSON array if role=assistant + tool calls
    name                   TEXT,   -- tool name for role=tool
    reasoning              TEXT,   -- extended thinking (free-form text)
    reasoning_details      TEXT,   -- II.6: JSON, OpenRouter/Nous structured array
    codex_reasoning_items  TEXT,   -- II.6: JSON, OpenAI o1/o3 reasoning items
    timestamp              REAL NOT NULL,
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

-- Phase 11d: episodic memory — one row per completed turn with a short summary
-- of what happened (tools called, files touched, gist of the assistant reply).
-- Distinct from `messages_fts` (which indexes raw message content) — episodic
-- events are denormalised summaries optimised for "remind me what we decided
-- about X" queries across many sessions.
CREATE TABLE IF NOT EXISTS episodic_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    turn_index  INTEGER NOT NULL,
    summary     TEXT NOT NULL,
    tools_used  TEXT,         -- comma-separated tool names
    file_paths  TEXT,         -- comma-separated paths the turn touched
    timestamp   REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_episodic_session ON episodic_events(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_episodic_timestamp ON episodic_events(timestamp DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS episodic_fts USING fts5(
    summary,
    tools_used,
    file_paths,
    content='episodic_events',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS episodic_fts_insert
AFTER INSERT ON episodic_events BEGIN
    INSERT INTO episodic_fts(rowid, summary, tools_used, file_paths)
    VALUES (new.id, new.summary, new.tools_used, new.file_paths);
END;

CREATE TRIGGER IF NOT EXISTS episodic_fts_delete
AFTER DELETE ON episodic_events BEGIN
    INSERT INTO episodic_fts(episodic_fts, rowid, summary, tools_used, file_paths)
    VALUES ('delete', old.id, old.summary, old.tools_used, old.file_paths);
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
            current = int(row[0]) if row is not None else 0
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
                current = SCHEMA_VERSION

            # v1 → v2 (II.6): reasoning_details + codex_reasoning_items.
            # Pre-existing DBs only carry ``reasoning TEXT`` on messages;
            # ALTER TABLE adds the two new columns so round-tripping of
            # OpenRouter-style reasoning metadata works after upgrade.
            # SQLite ALTER TABLE ADD COLUMN is non-destructive and fast
            # (no table rewrite) — safe to run on large legacy DBs.
            if current < 2:
                for col_name in ("reasoning_details", "codex_reasoning_items"):
                    try:
                        conn.execute(
                            f'ALTER TABLE messages ADD COLUMN "{col_name}" TEXT'
                        )
                    except sqlite3.OperationalError:
                        # Column already exists (e.g. fresh DB built from
                        # the v2 DDL above, or prior partial migration).
                        pass
                conn.execute(
                    "UPDATE schema_version SET version = ?",
                    (SCHEMA_VERSION,),
                )

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
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
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
                [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls]
            )
            if msg.tool_calls
            else None
        )
        # II.6: reasoning structured fields serialise as JSON.
        # ``None`` (no reasoning, or non-reasoning provider) stays NULL;
        # non-None lists/dicts are JSON-dumped so ``get_messages`` can
        # load them back with ``json.loads``. No fallback coercion — if
        # a caller passes an un-JSON-able object, let the error surface.
        reasoning_details_json = (
            json.dumps(msg.reasoning_details)
            if msg.reasoning_details is not None
            else None
        )
        codex_items_json = (
            json.dumps(msg.codex_reasoning_items)
            if msg.codex_reasoning_items is not None
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
            reasoning_details_json,
            codex_items_json,
            time.time(),
        )

    #: Shared INSERT statement for the messages table. Kept as a module
    #: constant so ``append_message`` + ``append_messages_batch`` agree
    #: on column order — mismatch is a class of bug worth designing out.
    _INSERT_MESSAGE_SQL = (
        "INSERT INTO messages "
        "(session_id, role, content, tool_call_id, tool_calls, name, "
        "reasoning, reasoning_details, codex_reasoning_items, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    def append_message(self, session_id: str, msg: Message) -> int:
        with self._txn() as conn:
            cur = conn.execute(
                self._INSERT_MESSAGE_SQL,
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
                    self._INSERT_MESSAGE_SQL,
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
                "SELECT role, content, tool_call_id, tool_calls, name, "
                "reasoning, reasoning_details, codex_reasoning_items "
                "FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        out: list[Message] = []
        for r in rows:
            tool_calls = None
            if r["tool_calls"]:
                raw = json.loads(r["tool_calls"])
                tool_calls = [
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"]) for tc in raw
                ]
            # II.6: deserialise reasoning_details / codex_reasoning_items
            # if present. Tolerate bad JSON defensively — a corrupt column
            # should never break conversation resume, just drop the field.
            reasoning_details: Any = None
            if r["reasoning_details"]:
                try:
                    reasoning_details = json.loads(r["reasoning_details"])
                except (json.JSONDecodeError, TypeError):
                    reasoning_details = None
            codex_items: Any = None
            if r["codex_reasoning_items"]:
                try:
                    codex_items = json.loads(r["codex_reasoning_items"])
                except (json.JSONDecodeError, TypeError):
                    codex_items = None
            out.append(
                Message(
                    role=r["role"],
                    content=r["content"],
                    tool_call_id=r["tool_call_id"],
                    tool_calls=tool_calls,
                    name=r["name"],
                    reasoning=r["reasoning"],
                    reasoning_details=reasoning_details,
                    codex_reasoning_items=codex_items,
                )
            )
        return out

    # ─── episodic memory (Phase 11d) ──────────────────────────────

    def record_episodic(
        self,
        *,
        session_id: str,
        turn_index: int,
        summary: str,
        tools_used: list[str] | None = None,
        file_paths: list[str] | None = None,
    ) -> int:
        """Append one episodic event for a completed turn. Returns rowid."""
        tools_str = ",".join(tools_used) if tools_used else None
        files_str = ",".join(file_paths) if file_paths else None
        with self._txn() as conn:
            cur = conn.execute(
                "INSERT INTO episodic_events "
                "(session_id, turn_index, summary, tools_used, file_paths, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, turn_index, summary, tools_str, files_str, time.time()),
            )
            return int(cur.lastrowid or 0)

    def search_episodic(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """FTS5 search across all episodic events. Returns newest first.

        FTS5 reserves `.` as a column-qualifier separator, so queries like
        `auth.py` syntax-error without quoting. We always wrap in double
        quotes for safe phrase search.
        """
        stripped = query.strip()
        if not stripped:
            return []
        safe_q = '"' + stripped.replace('"', '""') + '"'
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.id, e.session_id, e.turn_index, e.summary, "
                "e.tools_used, e.file_paths, e.timestamp "
                "FROM episodic_fts "
                "JOIN episodic_events e ON e.id = episodic_fts.rowid "
                "WHERE episodic_fts MATCH ? "
                "ORDER BY e.timestamp DESC LIMIT ?",
                (safe_q, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_episodic(self, session_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List episodic events — for one session if provided, else newest across all."""
        with self._connect() as conn:
            if session_id is not None:
                rows = conn.execute(
                    "SELECT * FROM episodic_events WHERE session_id = ? "
                    "ORDER BY turn_index DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM episodic_events ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

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

    def search_messages(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Full-text search returning FULL message content (not snippet).

        Used by the SessionSearch agent tool. Differs from search() in that
        it returns the entire message text, letting the agent see the full
        surrounding context rather than a highlighted fragment.
        """
        safe_q = query.replace('"', '""').strip()
        if not safe_q:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.session_id, m.role, m.timestamp, m.content "
                "FROM messages_fts "
                "JOIN messages m ON m.id = messages_fts.rowid "
                "WHERE messages_fts MATCH ? "
                "ORDER BY m.timestamp DESC LIMIT ?",
                (safe_q, limit),
            ).fetchall()
            return [dict(r) for r in rows]


__all__ = ["SessionDB"]
