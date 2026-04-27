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
#: time advances the DB from its stored version to :data:`SCHEMA_VERSION`
#: via :func:`apply_migrations`. v1 = baseline (sessions/messages/FTS/
#: episodic). v2 = II.6 reasoning-chain metadata columns on ``messages``.
#: v3 = F1 consent layer tables (consent_grants, consent_counters,
#: audit_log). v4 = Round 2A P-18 episodic-memory dreaming column
#: (``dreamed_into`` on ``episodic_events`` — points an entry that has
#: been folded into a consolidation row at the row id of that
#: consolidation). Existing rows keep their data — new columns default
#: to NULL. v5 = Tier-A item 11 ``tool_usage`` table — per-tool-call
#: telemetry for ``opencomputer insights`` (tool, duration_ms, error,
#: model, ts). Existing data unaffected; the table starts empty.
SCHEMA_VERSION = 5

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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    turn_index    INTEGER NOT NULL,
    summary       TEXT NOT NULL,
    tools_used    TEXT,         -- comma-separated tool names
    file_paths    TEXT,         -- comma-separated paths the turn touched
    timestamp     REAL NOT NULL,
    -- Round 2A P-18: when this row has been folded into a dreaming
    -- consolidation, ``dreamed_into`` points at the consolidation's
    -- ``episodic_events.id``. NULL = not yet dreamed (re-run candidate).
    -- Consolidation rows themselves keep ``dreamed_into = NULL`` so they
    -- can be re-summarised in a later pass if desired.
    dreamed_into  INTEGER,
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

# ─── F1 (Sub-project F, phase 1): consent layer tables ────────────────
# Added in schema v3. See ~/.claude/plans/i-want-you-to-twinkly-squirrel.md
# for the full design rationale.
V3_CONSENT_DDL = """
CREATE TABLE IF NOT EXISTS consent_grants (
    capability_id   TEXT NOT NULL,
    scope_filter    TEXT,
    tier            INTEGER NOT NULL,
    granted_at      REAL NOT NULL,
    expires_at      REAL,
    granted_by      TEXT NOT NULL,
    PRIMARY KEY (capability_id, scope_filter)
);

CREATE TABLE IF NOT EXISTS consent_counters (
    capability_id   TEXT NOT NULL,
    scope_filter    TEXT,
    clean_run_count INTEGER NOT NULL DEFAULT 0,
    last_updated    REAL NOT NULL,
    PRIMARY KEY (capability_id, scope_filter)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT,
    timestamp       REAL NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    capability_id   TEXT NOT NULL,
    tier            INTEGER NOT NULL,
    scope           TEXT,
    decision        TEXT NOT NULL,
    reason          TEXT,
    prev_hmac       TEXT NOT NULL,
    row_hmac        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_cap
    ON audit_log(capability_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_session
    ON audit_log(session_id, timestamp);

-- Tamper-EVIDENCE (not tamper-proof): these triggers block writes via
-- the SQLite engine. FS-level tampering (rm, dd, bytewise edit) is
-- caught by AuditLogger.verify_chain() via the HMAC-SHA256 chain.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
"""


# ─── Migration framework ──────────────────────────────────────────────

MIGRATIONS: dict[tuple[int, int], str] = {
    (0, 1): "_migrate_v0_to_v1",
    (1, 2): "_migrate_v1_to_v2",
    (2, 3): "_migrate_v2_to_v3",
    (3, 4): "_migrate_v3_to_v4",
    (4, 5): "_migrate_v4_to_v5",
}


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Return stored schema version. Returns 0 on fresh DBs (no table yet)."""
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _bump_schema_version(conn: sqlite3.Connection, v: int) -> None:
    """Replace the single schema_version row."""
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (?)", (v,))


def _migrate_v0_to_v1(conn: sqlite3.Connection) -> None:
    """Apply the baseline DDL — sessions, messages, FTS5, episodic.

    DDL is idempotent (IF NOT EXISTS everywhere). Fresh DBs get the
    latest-shape tables including II.6's ``reasoning_details`` and
    ``codex_reasoning_items`` columns on ``messages`` — the v1→v2 ALTER
    below is a no-op for fresh DBs and only fires on legacy (pre-II.6)
    DBs where those columns don't yet exist.
    """
    conn.executescript(DDL)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """II.6: add reasoning-chain metadata columns to ``messages``.

    Pre-II.6 DBs carry only ``reasoning TEXT``; ``reasoning_details``
    (OpenRouter-style structured array) and ``codex_reasoning_items``
    (OpenAI o1/o3 reasoning) arrived later. SQLite ALTER TABLE ADD
    COLUMN is non-destructive and fast (no table rewrite) — safe on
    large legacy DBs. Wrapped in try/except so fresh DBs that already
    have the columns from DDL get a silent no-op.
    """
    for col_name in ("reasoning_details", "codex_reasoning_items"):
        try:
            conn.execute(
                f'ALTER TABLE messages ADD COLUMN "{col_name}" TEXT'
            )
        except sqlite3.OperationalError:
            # Column already exists (fresh DB built from v1 DDL that
            # already carries these columns, or prior partial migration).
            pass


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """F1: add consent_grants, consent_counters, audit_log tables + triggers."""
    conn.executescript(V3_CONSENT_DDL)


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Round 2A P-18: add ``dreamed_into`` column to ``episodic_events``.

    Pre-P18 DBs only carry the (id, session_id, turn_index, summary,
    tools_used, file_paths, timestamp) columns. ``dreamed_into`` is
    NULLable so legacy rows are unaffected — every existing entry is
    "not yet dreamed", which is the correct semantic on first run of
    ``opencomputer memory dream-now`` after upgrade.

    Wrapped in try/except so fresh DBs already built from the v1 DDL
    that includes the new column (per the P-18 DDL update) get a silent
    no-op.
    """
    try:
        conn.execute("ALTER TABLE episodic_events ADD COLUMN dreamed_into INTEGER")
    except sqlite3.OperationalError:
        # Column already exists (fresh DB; or partial migration).
        pass


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Tier-A item 11: per-tool-call telemetry table.

    Records one row per tool dispatch — what tool, how long, did it
    error, which session/model. Powers ``opencomputer insights`` for
    the "is web_search costing me 60% of my time/spend?" answer that
    aggregate per-provider cost can't surface.

    Idempotent — fresh DBs and re-runs both no-op cleanly via
    ``IF NOT EXISTS``.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tool_usage (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT NOT NULL,
            ts            REAL NOT NULL,
            tool          TEXT NOT NULL,
            model         TEXT,
            duration_ms   REAL,
            error         INTEGER NOT NULL DEFAULT 0,
            outcome       TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tool_usage_session
            ON tool_usage(session_id);
        CREATE INDEX IF NOT EXISTS idx_tool_usage_ts
            ON tool_usage(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_tool_usage_tool
            ON tool_usage(tool);
        """
    )


#: Columns that historically arrived via numbered ALTER migrations.
#: We re-assert their presence on every connect so a DB whose
#: schema_version row was bumped without the corresponding ALTER firing
#: (cause: a partial migration on an older build, or hand-edited
#: schema_version) self-heals on next open instead of crashing the first
#: write that touches the missing column.
_EXPECTED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("messages", "reasoning_details", "TEXT"),
    ("messages", "codex_reasoning_items", "TEXT"),
    ("episodic_events", "dreamed_into", "INTEGER"),
)


def _self_heal_columns(conn: sqlite3.Connection) -> None:
    """Ensure every column in :data:`_EXPECTED_COLUMNS` exists.

    Defence-in-depth against stored ``schema_version`` lying about the
    physical schema. ``ALTER TABLE ADD COLUMN`` is fast and
    non-destructive on SQLite (no table rewrite). We skip cleanly when:

    - the target table doesn't exist yet (legacy DBs that pre-date a
      table — the relevant numbered migration will create it on next
      bump, not this self-heal), or
    - the column already exists ("duplicate column name").

    Any other ``OperationalError`` propagates so genuine schema bugs
    surface in tests instead of being masked.
    """
    for table, column, sql_type in _EXPECTED_COLUMNS:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if cur.fetchone() is None:
            continue
        try:
            conn.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}'
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Advance DB from stored schema_version to SCHEMA_VERSION. Idempotent."""
    current = _read_schema_version(conn)
    while current < SCHEMA_VERSION:
        fn_name = MIGRATIONS[(current, current + 1)]
        globals()[fn_name](conn)
        _bump_schema_version(conn, current + 1)
        current += 1
    _self_heal_columns(conn)
    conn.commit()


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
            apply_migrations(conn)

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
        # Round 2B P-4 — bind the session id onto the
        # observability ContextVar so subsequent log records emitted
        # from this coroutine carry it. Import is local so the SessionDB
        # has no hard dependency on the observability module (keeps the
        # state.py surface lean and avoids any import-cycle risk).
        try:
            from opencomputer.observability.logging_config import set_session_id

            set_session_id(session_id)
        except Exception:  # noqa: BLE001 — never let logging glue break sessions
            pass

    def end_session(self, session_id: str) -> None:
        with self._txn() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (time.time(), session_id),
            )
        # Clear the observability ContextVar so the next session in this
        # coroutine doesn't inherit our id — verified via /ultrareview
        # runtime test that the gateway daemon's per-message
        # ``handle_message`` coroutine reuses the same context across
        # sessions, leaking session_id into subsequent logs without this
        # reset. Mirrors the create_session/set_session_id pattern.
        try:
            from opencomputer.observability.logging_config import set_session_id

            set_session_id(None)
        except Exception:  # noqa: BLE001 — never let logging glue break sessions
            pass

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

    # ─── episodic dreaming (Round 2A P-18) ────────────────────────

    def list_undreamed_episodic(
        self, session_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return episodic rows with ``dreamed_into IS NULL`` ordered oldest-first.

        Oldest-first ordering matters for clustering: dreaming groups by
        date bucket + topic-keyword overlap, so we want chronologically
        adjacent entries to land in the same cluster naturally.

        Consolidation rows themselves (``turn_index = -1``) are
        excluded so they aren't re-summarised into super-summaries on
        every subsequent ``dream-now`` pass — a recursive consolidation
        path is left open for a future "compact" sub-feature with its
        own knobs.
        """
        with self._connect() as conn:
            if session_id is not None:
                rows = conn.execute(
                    "SELECT * FROM episodic_events "
                    "WHERE session_id = ? AND dreamed_into IS NULL "
                    "AND turn_index >= 0 "
                    "ORDER BY timestamp ASC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM episodic_events "
                    "WHERE dreamed_into IS NULL "
                    "AND turn_index >= 0 "
                    "ORDER BY timestamp ASC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def record_dream_consolidation(
        self,
        *,
        session_id: str,
        summary: str,
        source_event_ids: list[int],
        tools_used: list[str] | None = None,
        file_paths: list[str] | None = None,
    ) -> int:
        """Atomically write a consolidation row and stamp originals with ``dreamed_into``.

        The consolidation row itself is a regular ``episodic_events`` entry
        with ``dreamed_into = NULL`` (so it stays searchable via the
        existing FTS5 index) but its ``turn_index`` is set to ``-1`` to
        flag it as agent-generated rather than a recorded user turn.

        ``source_event_ids`` are updated in the same transaction so a
        crash mid-way leaves the originals undreamed (idempotent re-run
        will pick them up).
        """
        tools_str = ",".join(tools_used) if tools_used else None
        files_str = ",".join(file_paths) if file_paths else None
        with self._txn() as conn:
            cur = conn.execute(
                "INSERT INTO episodic_events "
                "(session_id, turn_index, summary, tools_used, file_paths, timestamp, dreamed_into) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (session_id, -1, summary, tools_str, files_str, time.time()),
            )
            consolidation_id = int(cur.lastrowid or 0)
            if source_event_ids:
                placeholders = ",".join("?" * len(source_event_ids))
                conn.execute(
                    f"UPDATE episodic_events SET dreamed_into = ? "
                    f"WHERE id IN ({placeholders})",
                    (consolidation_id, *source_event_ids),
                )
            return consolidation_id

    # ─── FTS5 search ──────────────────────────────────────────────

    def search(
        self, query: str, limit: int = 20, *, phrase: bool = False
    ) -> list[dict[str, Any]]:
        """Full-text search across all messages. Returns snippet + metadata.

        ``phrase=False`` (default) preserves the legacy behaviour: caller
        is responsible for FTS5 syntax. Internal escaping only doubles
        embedded ``"`` so the input doesn't break the SQL parser.
        Existing callers (``mcp/server.py`` documents "FTS5 syntax";
        ``tools/recall.py``) rely on this — changing the default would
        be a silent behaviour change.

        ``phrase=True`` wraps the entire query as a single FTS5 phrase
        (``"…"``) so reserved characters (``:``, ``*``, ``(``, ``)``,
        ``AND``/``OR``/``NOT``) are treated as literal text. Use this
        for direct user input (e.g. CLI ``--search`` flag, P-12).
        Mirrors the pattern :meth:`search_episodic` already uses.
        """
        stripped = query.strip()
        if not stripped:
            return []
        # phrase=True: wrap as `"…"` so FTS5 reserved chars stay literal.
        # phrase=False: legacy — only escape internal `"` for SQL safety;
        # FTS5 will reject malformed queries.
        escaped = stripped.replace('"', '""')
        safe_q = f'"{escaped}"' if phrase else escaped
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

    # ─── Tier-A item 11: tool_usage telemetry ─────────────────────

    def record_tool_usage(
        self,
        *,
        session_id: str,
        tool: str,
        outcome: str,
        duration_ms: float | None = None,
        model: str | None = None,
        ts: float | None = None,
    ) -> None:
        """Record one tool dispatch into the v5 ``tool_usage`` table.

        Args:
            session_id: Owner session.
            tool: Tool name (``"Read"``, ``"Bash"``, ``"WebSearch"``, …).
            outcome: One of ``success``, ``failure``, ``blocked``,
                ``cancelled``. ``error`` column is set to 1 for everything
                except ``success`` so simple "% errored" queries are cheap.
            duration_ms: Wall-clock spent in ``tool.execute`` (in ms). May
                be ``None`` for very fast tools where measurement noise
                dwarfs the value.
            model: The LLM model whose response triggered this tool call,
                if known. Best-effort attribution — not all dispatch sites
                pass it.
            ts: Override timestamp (UTC seconds). Defaults to ``time.time()``.

        Failures here are swallowed: telemetry should never break the loop.
        """
        try:
            err = 0 if outcome == "success" else 1
            with self._txn() as conn:
                conn.execute(
                    "INSERT INTO tool_usage "
                    "(session_id, ts, tool, model, duration_ms, error, outcome) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        ts if ts is not None else time.time(),
                        tool,
                        model,
                        duration_ms,
                        err,
                        outcome,
                    ),
                )
        except sqlite3.OperationalError:
            # Pre-v5 DB or transient lock. Telemetry is best-effort — drop
            # this row rather than break the dispatch path.
            pass

    def query_tool_usage(
        self,
        *,
        days: int | None = 30,
        group_by: str = "tool",
    ) -> list[dict[str, Any]]:
        """Aggregate ``tool_usage`` rows for the insights CLI.

        Args:
            days: Time window — only rows newer than ``now - days * 86400``.
                Pass ``None`` for "all time".
            group_by: ``tool`` | ``model`` | ``session_id``. Anything else
                is treated as ``tool``.

        Returns:
            Rows like ``[{"key": "Read", "calls": 42, "errors": 1,
            "avg_duration_ms": 12.3, "total_duration_ms": 516.6,
            "error_rate": 0.024}, ...]`` sorted by ``calls`` desc.
        """
        col = group_by if group_by in ("tool", "model", "session_id") else "tool"
        params: list[Any] = []
        sql = (
            f"SELECT {col} as key, "
            "COUNT(*) as calls, "
            "SUM(error) as errors, "
            "AVG(duration_ms) as avg_duration_ms, "
            "SUM(duration_ms) as total_duration_ms "
            "FROM tool_usage "
        )
        if days is not None:
            sql += "WHERE ts >= ? "
            params.append(time.time() - days * 86400)
        sql += f"GROUP BY {col} ORDER BY calls DESC"

        with self._connect() as conn:
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                # Pre-v5 DB; return empty.
                return []

        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            calls = d.get("calls") or 0
            errs = d.get("errors") or 0
            d["error_rate"] = (errs / calls) if calls else 0.0
            out.append(d)
        return out


__all__ = ["SessionDB"]
