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
SCHEMA_VERSION = 9

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
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens  INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    vibe          TEXT,    -- A.4 (2026-04-27): per-session emotional state
                           -- (frustrated|excited|tired|curious|calm|stuck|"")
    vibe_updated  REAL,    -- A.4: when vibe was last classified (epoch seconds)
    cwd           TEXT     -- Plan 3 (2026-05-01): working dir at session start,
                           -- input signal for profile_analysis_daily cwd-clusterer
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
    reasoning_replay_blocks TEXT,  -- 2026-05-02: JSON, verbatim provider replay blocks (Anthropic thinking with signatures)
    attachments            TEXT,   -- 2026-04-27: JSON list[str], image attachments
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
    (5, 6): "_migrate_v5_to_v6",
    (6, 7): "_migrate_v6_to_v7",
    (7, 8): "_migrate_v7_to_v8",
    (8, 9): "_migrate_v8_to_v9",
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
    for col_name in (
        "reasoning_details",
        "codex_reasoning_items",
        "reasoning_replay_blocks",  # 2026-05-02 — verbatim provider replay blocks
        "attachments",
    ):
        try:
            conn.execute(
                f'ALTER TABLE messages ADD COLUMN "{col_name}" TEXT'
            )
        except sqlite3.OperationalError:
            # Column already exists (fresh DB built from v1 DDL that
            # already carries these columns, or prior partial migration).
            pass
    # A.4 (2026-04-27): per-session vibe column for the companion mood
    # thread. ``vibe`` is one of {frustrated, excited, tired, curious,
    # calm, stuck} or NULL (not yet classified). ``vibe_updated`` is the
    # epoch timestamp of the last classification.
    for col_def in (
        ("sessions", "vibe", "TEXT"),
        ("sessions", "vibe_updated", "REAL"),
    ):
        table, col, typ = col_def
        try:
            conn.execute(
                f'ALTER TABLE {table} ADD COLUMN "{col}" {typ}'
            )
        except sqlite3.OperationalError:
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


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Per-message vibe verdict log (2026-04-28).

    The session-level ``sessions.vibe`` column only retains the most-recent
    verdict and was previously gated behind the companion-persona overlay,
    so production carried zero per-turn evidence to evaluate the
    classifier against. ``vibe_log`` keeps every verdict — one row per
    user turn — tagged with ``classifier_version`` so a future swap
    (regex → embedding/LLM) can A/B against the existing baseline.

    ``message_id`` is nullable: we record the verdict in the same lane
    where the user message has just been written, but the FK is loose so
    a cleanup of legacy messages does not orphan classifier evidence.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS vibe_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id          TEXT NOT NULL,
            message_id          INTEGER,
            vibe                TEXT NOT NULL,
            classifier_version  TEXT NOT NULL,
            timestamp           REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_vibe_log_session
            ON vibe_log(session_id, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_vibe_log_classifier
            ON vibe_log(classifier_version, timestamp DESC);
        """
    )


def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """Phase 0 of outcome-aware learning (2026-05-03).

    Adds two new tables:

    1. ``turn_outcomes`` — one row per completed assistant turn, capturing
       implicit signals (tool success/failure counts, vibe before/after,
       reply latency, affirmation/correction regex hits, abandonment flag,
       standing-order violations). Phase 1 layers ``composite_score`` /
       ``judge_score`` / ``turn_score`` columns on top via migration v8.

    2. ``recall_citations`` — links a turn to each memory the recall tool
       returned for it. Phase 2 v0's recommendation engine
       (``MostCitedBelowMedian/1``) joins on this table to compute mean
       downstream ``turn_score`` per memory. Without it the engine cannot
       distinguish "memory M was actually surfaced in turn T" from "memory
       M happens to share a session_id with turn T."

    Both tables CASCADE on ``sessions`` deletion (consistent with messages
    and episodic_events FK behavior). ``turn_outcomes.schema_version``
    carries an internal mini-version so a future column reshuffle can
    branch on it without bumping the global SCHEMA_VERSION twice.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS turn_outcomes (
            id                          TEXT PRIMARY KEY,
            session_id                  TEXT NOT NULL,
            turn_index                  INTEGER NOT NULL,
            created_at                  REAL NOT NULL,
            tool_call_count             INTEGER DEFAULT 0,
            tool_success_count          INTEGER DEFAULT 0,
            tool_error_count            INTEGER DEFAULT 0,
            tool_blocked_count          INTEGER DEFAULT 0,
            self_cancel_count           INTEGER DEFAULT 0,
            retry_count                 INTEGER DEFAULT 0,
            vibe_before                 TEXT,
            vibe_after                  TEXT,
            reply_latency_s             REAL,
            affirmation_present         INTEGER DEFAULT 0,
            correction_present          INTEGER DEFAULT 0,
            conversation_abandoned      INTEGER DEFAULT 0,
            standing_order_violations   TEXT,
            duration_s                  REAL,
            schema_version              INTEGER DEFAULT 1,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_turn_outcomes_session
            ON turn_outcomes(session_id, turn_index);
        CREATE INDEX IF NOT EXISTS idx_turn_outcomes_created
            ON turn_outcomes(created_at);

        CREATE TABLE IF NOT EXISTS recall_citations (
            id                          TEXT PRIMARY KEY,
            session_id                  TEXT NOT NULL,
            turn_index                  INTEGER NOT NULL,
            episodic_event_id           TEXT,
            candidate_kind              TEXT NOT NULL,
            candidate_text_id           TEXT,
            bm25_score                  REAL,
            adjusted_score              REAL,
            retrieved_at                REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_recall_citations_episodic
            ON recall_citations(episodic_event_id);
        CREATE INDEX IF NOT EXISTS idx_recall_citations_session_turn
            ON recall_citations(session_id, turn_index);
        """
    )


def _migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    """Phase 1 of outcome-aware learning (2026-05-03).

    Adds the scoring columns on top of Phase 0's ``turn_outcomes`` table.
    Composite score is purely arithmetic (no LLM); judge_* columns are
    populated by the cheap LLM judge in ``agent/reviewer.py`` when budget
    allows. ``turn_score`` is the fused 0.4*composite + 0.6*judge.

    All columns nullable so partial Phase 0 / Phase 1 deployments
    coexist — Phase 1 simply doesn't fill them yet.
    """
    for col, typ in (
        ("composite_score", "REAL"),
        ("judge_score", "REAL"),
        ("judge_reasoning", "TEXT"),
        ("judge_model", "TEXT"),
        ("turn_score", "REAL"),
        ("scored_at", "REAL"),
    ):
        try:
            conn.execute(f'ALTER TABLE turn_outcomes ADD COLUMN "{col}" {typ}')
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    """Phase 2 v0 of outcome-aware learning (2026-05-03).

    Two changes:

    1. ``episodic_events`` gets ``recall_penalty REAL DEFAULT 0.0`` and
       ``recall_penalty_updated_at REAL`` columns. The recall pipeline
       multiplies BM25 score by ``max(0.05, 1 - penalty * decay(age))`` so
       penalised memories are suppressed but never literally unreachable.

    2. ``policy_changes`` table — HMAC-chained audit log for every
       reversible policy decision the engine makes. Mirrors the consent
       audit pattern (prev_hmac → row_hmac chain, verify_chain detects
       tamper). Status field tracks the lifecycle:
       drafted → pending_approval | pending_evaluation → active | reverted
                                                         | expired_decayed
    """
    for col, typ in (
        ("recall_penalty", "REAL DEFAULT 0.0"),
        ("recall_penalty_updated_at", "REAL"),
    ):
        try:
            conn.execute(
                f'ALTER TABLE episodic_events ADD COLUMN "{col}" {typ}'
            )
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            # Tolerate "no such table" (legacy DB upgrade path where
            # episodic_events was never created by v0→v1 baseline) and
            # "duplicate column name" (idempotent re-run). Anything else
            # is a genuine schema bug.
            if "duplicate column name" not in msg and "no such table" not in msg:
                raise

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS policy_changes (
            id                              TEXT PRIMARY KEY,
            ts_drafted                      REAL NOT NULL,
            ts_applied                      REAL,
            knob_kind                       TEXT NOT NULL,
            target_id                       TEXT NOT NULL,
            prev_value                      TEXT NOT NULL,
            new_value                       TEXT NOT NULL,
            reason                          TEXT NOT NULL,
            expected_effect                 TEXT,
            revert_after                    REAL,
            rollback_hook                   TEXT NOT NULL,
            recommendation_engine_version   TEXT NOT NULL,
            approval_mode                   TEXT NOT NULL,
            approved_by                     TEXT,
            approved_at                     REAL,
            hmac_prev                       TEXT NOT NULL,
            hmac_self                       TEXT NOT NULL,
            status                          TEXT NOT NULL,
            eligible_turn_count             INTEGER DEFAULT 0,
            pre_change_baseline_mean        REAL,
            pre_change_baseline_std         REAL,
            post_change_mean                REAL,
            reverted_at                     REAL,
            reverted_reason                 TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_policy_changes_status
            ON policy_changes(status);
        CREATE INDEX IF NOT EXISTS idx_policy_changes_target
            ON policy_changes(knob_kind, target_id);
        CREATE INDEX IF NOT EXISTS idx_policy_changes_engine
            ON policy_changes(recommendation_engine_version);
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
    ("messages", "reasoning_replay_blocks", "TEXT"),  # 2026-05-02
    ("messages", "attachments", "TEXT"),
    ("sessions", "cache_read_tokens", "INTEGER DEFAULT 0"),  # 2026-05-02
    ("sessions", "cache_write_tokens", "INTEGER DEFAULT 0"),  # 2026-05-02
    ("sessions", "vibe", "TEXT"),
    ("sessions", "vibe_updated", "REAL"),
    ("sessions", "cwd", "TEXT"),  # Plan 3 (2026-05-01) — profile-suggester input
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
        self,
        session_id: str,
        platform: str = "cli",
        model: str = "",
        title: str = "",
        cwd: str | None = None,  # Plan 3 — captured for profile-suggester
    ) -> None:
        """Create or upsert a session row.

        Uses ON CONFLICT DO UPDATE rather than INSERT OR REPLACE so a
        pre-existing row's ``title`` survives — important when ``/rename``
        ran before the user's first message and pre-created the row via
        :meth:`set_session_title`. Other metadata (``started_at``,
        ``platform``, ``model``, ``cwd``) is updated to current values,
        which matches what callers expect when they invoke this.

        Plan 3 (2026-05-01): ``cwd`` is the working directory the user
        was in when ``oc`` started — used by the daily profile-analysis
        cron to detect cwd patterns. Defaults to None for backwards
        compatibility with callers that don't pass it.
        """
        with self._txn() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, started_at, platform, model, title, cwd)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  started_at = excluded.started_at,
                  platform   = excluded.platform,
                  model      = excluded.model,
                  cwd        = excluded.cwd
                """,
                (session_id, time.time(), platform, model, title, cwd),
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

    def count_sessions(self) -> int:
        """Total session count. Used by the learning-moments
        returning-user seed to decide whether the user has enough
        history to skip the v1 reveal cycle."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        return int(row[0]) if row else 0

    def first_session_started_at(self) -> float | None:
        """Epoch seconds of the earliest session, or ``None`` if no
        sessions yet. Used by ``learning_moments`` to compute
        ``days_since_first_session`` for established-user gates."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(started_at) FROM sessions"
            ).fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])

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

    # ─── session titles (TS-T6) ───────────────────────────────────

    def get_session_title(self, session_id: str) -> str | None:
        """Return the session's stored title, or None if unset.

        Treats the empty string the same as NULL — ``create_session``
        seeds the column with ``""`` for sessions that have never been
        titled, so we collapse both to ``None`` for callers that just
        want to know "is there a title yet?".
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        title = row["title"]
        return title if title else None

    def set_session_title(self, session_id: str, title: str) -> None:
        """Persist ``title`` on the session row, creating a minimal row
        if it doesn't exist yet.

        Why upsert: ``/rename`` can fire BEFORE the user's first message
        (which is what triggers :meth:`create_session`). A bare UPDATE
        would silently no-op against a missing row — the slash-handler
        would print "session renamed →" while nothing actually changed
        in the DB, and the title indicator would never appear. The
        ``ON CONFLICT DO UPDATE`` keeps the post-create-session case
        atomic too (idempotent across re-runs).

        :meth:`create_session` is the matching half — it now uses an
        UPSERT that *preserves* an existing title, so a row pre-created
        by ``/rename`` survives the first turn intact.

        Called from a daemon thread by
        :func:`opencomputer.agent.title_generator.maybe_auto_title`, so
        we use the same ``_txn`` retry-on-busy wrapper as every other
        write — SQLite handles concurrent writers from multiple
        connections via WAL + ``BEGIN IMMEDIATE``.
        """
        with self._txn() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, started_at, platform, model, title)
                VALUES (?, ?, '', '', ?)
                ON CONFLICT(id) DO UPDATE SET title = excluded.title
                """,
                (session_id, time.time(), title),
            )

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and every row that cascades from it.

        Returns ``True`` if a session row was removed, ``False`` if no
        session had that id.

        Cascades automatically — every child table has
        ``FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE``
        and ``PRAGMA foreign_keys=ON`` is set on every connection
        (line 448), so the single parent delete cleans up:

            - messages → messages_fts (FTS delete trigger fires on cascade)
            - episodic_events → episodic_fts
            - vibe_log
            - tool_usage

        Untouched (by design):

            - audit_log (F1: append-only by trigger; tamper-evident)
            - consent_grants / consent_counters (per-capability scope,
              not per-session)
        """
        with self._txn() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            )
            deleted = cur.rowcount > 0
        return deleted

    def auto_prune(
        self,
        *,
        older_than_days: int,
        untitled_days: int,
        min_messages: int,
        cap: int = 200,
    ) -> int:
        """Delete stale sessions matching either of two policies.

        Policy A: any session whose ``started_at`` is older than
                  ``older_than_days`` days. Disabled when set to 0.
        Policy B: untitled sessions with fewer than ``min_messages``
                  messages whose ``started_at`` is older than
                  ``untitled_days`` days. Disabled when ``untitled_days``
                  is 0.

        Either or both policies may be active. The two clauses combine
        with SQL ``OR`` so we run a single SELECT.

        Caps deletion at ``cap`` rows per call to keep startup fast.
        Returns the count of sessions actually removed.
        """
        if older_than_days <= 0 and untitled_days <= 0:
            return 0
        now = time.time()
        clauses: list[str] = []
        params: list[Any] = []
        if older_than_days > 0:
            clauses.append("started_at < ?")
            params.append(now - older_than_days * 86400)
        if untitled_days > 0:
            clauses.append(
                "(started_at < ? AND (title IS NULL OR title = '') "
                "AND COALESCE(message_count, 0) < ?)"
            )
            params.append(now - untitled_days * 86400)
            params.append(min_messages)
        where = " OR ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM sessions WHERE {where} LIMIT ?",
                (*params, cap),
            ).fetchall()
        deleted = 0
        for (sid,) in rows:
            if self.delete_session(sid):
                deleted += 1
        return deleted

    # ─── A.4 mood thread (2026-04-27) ─────────────────────────────

    def get_session_vibe(self, session_id: str) -> tuple[str | None, float | None]:
        """Return ``(vibe, last_updated_epoch)`` for the session.

        ``vibe`` is one of ``frustrated|excited|tired|curious|calm|stuck``
        or ``None`` if not yet classified. ``last_updated_epoch`` is the
        ``time.time()`` value when the classifier last wrote, or None.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT vibe, vibe_updated FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return (None, None)
        vibe = row["vibe"]
        ts = row["vibe_updated"]
        return (vibe if vibe else None, float(ts) if ts is not None else None)

    def set_session_vibe(self, session_id: str, vibe: str) -> None:
        """Persist ``vibe`` (and stamp ``vibe_updated`` = now) on the session.

        Vibes are advisory companion-context markers — wrong values won't
        crash anything, the companion overlay just gets bad anchors.
        Caller is responsible for picking from the supported vocabulary.
        """
        with self._txn() as conn:
            conn.execute(
                "UPDATE sessions SET vibe = ?, vibe_updated = ? WHERE id = ?",
                (vibe, time.time(), session_id),
            )

    def record_vibe(
        self,
        session_id: str,
        vibe: str,
        *,
        classifier_version: str = "regex_v1",
        message_id: int | None = None,
        timestamp: float | None = None,
    ) -> int:
        """Append one row to ``vibe_log``.

        Returns the inserted row id. Caller should pass ``classifier_version``
        whenever a new backend ships (e.g. ``"embed_v1"``) so offline A/B
        analysis can partition by source. ``message_id`` is optional —
        when omitted the latest user-role message id for the session is
        looked up so the log entry still anchors to a turn.
        """
        ts = float(timestamp) if timestamp is not None else time.time()
        with self._txn() as conn:
            if message_id is None:
                row = conn.execute(
                    "SELECT id FROM messages "
                    "WHERE session_id = ? AND role = 'user' "
                    "ORDER BY id DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                resolved_message_id = int(row[0]) if row is not None else None
            else:
                resolved_message_id = int(message_id)
            cur = conn.execute(
                "INSERT INTO vibe_log "
                "(session_id, message_id, vibe, classifier_version, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, resolved_message_id, vibe, classifier_version, ts),
            )
            return int(cur.lastrowid or 0)

    def list_vibe_log_for_session(
        self, session_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return per-turn vibe verdicts for a session, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, session_id, message_id, vibe, classifier_version, timestamp "
                "FROM vibe_log WHERE session_id = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_recent_session_vibes(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return the N most-recent sessions that have a vibe classified.

        Used by the companion overlay to surface "you sounded frustrated
        yesterday" — the agent can reference the previous-session vibe
        when the user returns.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, vibe, vibe_updated, started_at "
                "FROM sessions "
                "WHERE vibe IS NOT NULL AND vibe != '' "
                "ORDER BY vibe_updated DESC LIMIT ?",
                (limit,),
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
        # 2026-05-02: verbatim provider replay blocks (Anthropic thinking
        # with signatures). Persisted as JSON so a mid-cycle session
        # resume retains the cryptographic signatures the API requires
        # alongside tool_result.
        reasoning_replay_json = (
            json.dumps(msg.reasoning_replay_blocks)
            if msg.reasoning_replay_blocks is not None
            else None
        )
        # 2026-04-27: image attachments serialise as JSON list[str].
        # Empty list → NULL so non-image messages don't bloat the column.
        attachments_json = (
            json.dumps(msg.attachments) if msg.attachments else None
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
            reasoning_replay_json,
            attachments_json,
            time.time(),
        )

    #: Shared INSERT statement for the messages table. Kept as a module
    #: constant so ``append_message`` + ``append_messages_batch`` agree
    #: on column order — mismatch is a class of bug worth designing out.
    _INSERT_MESSAGE_SQL = (
        "INSERT INTO messages "
        "(session_id, role, content, tool_call_id, tool_calls, name, "
        "reasoning, reasoning_details, codex_reasoning_items, "
        "reasoning_replay_blocks, attachments, "
        "timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
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

    def add_tokens(
        self,
        session_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Bump the per-session token counters by the given deltas.

        PR #221 follow-up Item 2 — wires real numbers into the
        ``input_tokens`` / ``output_tokens`` columns the schema has
        always reserved but that no UPDATE site populated until now.
        Callers (the agent loop) pass the per-turn deltas from
        :class:`plugin_sdk.provider_contract.Usage`. Negative values are
        clamped to ``0`` defensively — a buggy provider mustn't be able
        to drag the running total backwards.

        2026-05-02 — cache_read_tokens / cache_write_tokens accumulate
        prompt-cache hits + writes for ``/usage`` to surface. Default
        zero keeps every existing call site working unchanged.

        No-op when all deltas are zero (and when ``session_id`` is
        empty), so callers don't need to branch on the common case
        where a provider declined to surface usage.
        """
        if not session_id:
            return
        in_delta = max(0, int(input_tokens or 0))
        out_delta = max(0, int(output_tokens or 0))
        cr_delta = max(0, int(cache_read_tokens or 0))
        cw_delta = max(0, int(cache_write_tokens or 0))
        if in_delta == 0 and out_delta == 0 and cr_delta == 0 and cw_delta == 0:
            return
        with self._txn() as conn:
            conn.execute(
                "UPDATE sessions SET "
                "input_tokens = input_tokens + ?, "
                "output_tokens = output_tokens + ?, "
                "cache_read_tokens = cache_read_tokens + ?, "
                "cache_write_tokens = cache_write_tokens + ? "
                "WHERE id = ?",
                (in_delta, out_delta, cr_delta, cw_delta, session_id),
            )

    def get_messages(self, session_id: str) -> list[Message]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, tool_call_id, tool_calls, name, "
                "reasoning, reasoning_details, codex_reasoning_items, "
                "reasoning_replay_blocks, attachments "
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
            # 2026-05-02: deserialise reasoning_replay_blocks (Anthropic
            # thinking blocks with cryptographic signatures). Tolerate
            # missing column / bad JSON — pre-migration rows return None
            # which is the same as "no replay needed".
            reasoning_replay: Any = None
            try:
                raw_replay = r["reasoning_replay_blocks"]
            except (IndexError, KeyError):
                raw_replay = None
            if raw_replay:
                try:
                    reasoning_replay = json.loads(raw_replay)
                except (json.JSONDecodeError, TypeError):
                    reasoning_replay = None
            # 2026-04-27: deserialise attachments (image paths). Same
            # forgiving JSON shape as the reasoning fields. Pre-migration
            # rows return NULL via dict.get(); legacy DBs upgraded by
            # _self_heal_columns get an empty column.
            attachments_list: list[str] = []
            try:
                raw_attach = r["attachments"]
            except (IndexError, KeyError):
                raw_attach = None
            if raw_attach:
                try:
                    parsed = json.loads(raw_attach)
                    if isinstance(parsed, list):
                        attachments_list = [str(p) for p in parsed]
                except (json.JSONDecodeError, TypeError):
                    attachments_list = []
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
                    reasoning_replay_blocks=reasoning_replay,
                    attachments=attachments_list,
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


    # ─── Phase 0 outcome-aware learning helpers ─────────────────────

    def query_tool_usage_in_window(
        self,
        *,
        session_id: str,
        start_ts: float,
        end_ts: float,
    ) -> dict[str, int]:
        """Aggregate tool_usage counts within a turn's wall-clock window.

        Used by gateway/dispatch.py at end-of-turn to populate
        ``turn_outcomes.tool_*_count`` columns. Returns a dict with keys
        ``call_count``, ``success_count``, ``error_count``,
        ``blocked_count``. Pre-v5 DBs (no tool_usage table) return zeros.
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT outcome, error FROM tool_usage "
                    "WHERE session_id = ? AND ts >= ? AND ts <= ?",
                    (session_id, start_ts, end_ts),
                ).fetchall()
        except sqlite3.OperationalError:
            return {
                "call_count": 0,
                "success_count": 0,
                "error_count": 0,
                "blocked_count": 0,
            }
        call = len(rows)
        success = sum(1 for r in rows if r[0] == "success")
        blocked = sum(1 for r in rows if r[0] == "blocked")
        # ``error`` flag covers failure + cancelled + anything non-success.
        # We separate "blocked" (consent gate refusal) from "error" since
        # the composite scorer treats them differently — blocked is a
        # safety win, not a failure.
        error = sum(1 for r in rows if r[1] == 1 and r[0] != "blocked")
        return {
            "call_count": call,
            "success_count": success,
            "error_count": error,
            "blocked_count": blocked,
        }

    def query_recent_vibes(
        self,
        *,
        session_id: str,
        before_ts: float,
        limit: int = 2,
    ) -> list[str]:
        """Return up to ``limit`` most-recent vibe verdicts at or before
        ``before_ts``. Index 0 is the most recent. Empty list if none."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT vibe FROM vibe_log WHERE session_id = ? "
                "AND timestamp <= ? ORDER BY timestamp DESC LIMIT ?",
                (session_id, before_ts, limit),
            ).fetchall()
        return [r[0] for r in rows]


__all__ = ["SessionDB"]
