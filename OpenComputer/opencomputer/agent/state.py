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
import logging
import random
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plugin_sdk.core import Message, ToolCall

#: Logger for SessionDB internals. Module-level to keep call sites cheap;
#: callers do not configure handlers — that's owned by the host (CLI /
#: gateway). Severity discipline:
#:   - ``warning``: adversarial / out-of-band caller passed empty / unknown
#:     session ids; helpers self-heal but the caller likely has a bug.
#:   - ``error``: a SQL operation we expected to succeed raised; bubbled
#:     to the user via the empty-state path so the agent loop never wedges.
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionUsageRow:
    """Per-session usage snapshot — return value of
    :meth:`SessionDB.session_usage_summary` and
    :meth:`SessionDB.usage_summary_aggregate`.

    Frozen + slots-free for cheap equality + simple dict-like consumption
    by Rich / typer renderers. ``cost_usd`` is ``None`` whenever the
    underlying ``llm_calls`` rows for this session lack pricing data —
    surfacing ``0.0`` would be a lie because the cost-guard table doesn't
    cover every model. See CC visibility spec §4.2.
    """

    session_id: str
    model: str | None
    started_at: float
    ended_at: float | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    compactions_count: int
    cost_usd: float | None


@dataclass(frozen=True)
class PromptCheckpoint:
    """User-named session checkpoint backing ``/checkpoint`` + ``/restore``.

    Spec: docs/OC-FROM-CLAUDE-CODE.md §11.

    Distinct from the RewindStore (filesystem checkpoints powering
    ``/rollback``). Prompt checkpoints capture the message history at a
    chosen turn so the user can roll back to a known-good conversation
    state when the agent goes down a wrong path.

    Attributes:
        id: stable uuid4 string used by ``/restore <id>``.
        session_id: parent session FK; CASCADE-deletes with the session.
        prompt_index: integer offset (turn number) — surfaced in
            ``/restore`` listings so users can see "checkpoint after 5
            turns".
        messages: JSON-deserialised list of message dicts at the
            checkpoint moment. Each dict shape matches the wire format
            stored in ``messages``. None when the stored JSON is
            corrupt (the getter logs a warning and returns None in
            place of the row).
        files_snapshot: opt-in mapping of path → hash captured at
            checkpoint time. Off by default; enabled when the
            ``checkpoints.snapshot_files`` config is true. Provider-
            independent — the file content is NOT stored, just hashes
            for "did this file change" UX.
        label: human-readable handle. Non-unique — multiple checkpoints
            in a session can share a label (e.g. auto-labelled
            ``"before-Edit"``).
        created_at: epoch seconds.
    """

    id: str
    session_id: str
    prompt_index: int
    messages: list[dict[str, Any]]
    files_snapshot: dict[str, str] | None
    label: str
    created_at: float

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
#: v16 = delegate-lineage (2026-05-10) — ``sessions.parent_session_id``
#: + ``subagents`` table for cross-process registry persistence and
#: ``oc sessions tree`` lineage walks.
#: v17 = source-column (2026-05-10) — ``sessions.source`` so the
#: workspace sidebar can distinguish CLI/messaging/browser rows. Existing
#: rows are backfilled with ``'cli'`` (the historical de-facto source).
#: v18 = compactions-count (2026-05-10) — ``sessions.compactions_count``
#: tracks the number of times :class:`CompactionEngine` rewrote the
#: message history for a given session. Surfaced by ``/context`` (slash)
#: and ``oc context show`` / ``oc usage`` (CLIs) — closes the CC §4 +
#: §10 visibility gaps documented at
#: ``docs/superpowers/specs/2026-05-10-cc-usage-context-visibility-design.md``.
#: Additive nullable column with DEFAULT 0; legacy rows read 0.
#: v19 = git_branch (2026-05-11) — ``sessions.git_branch TEXT NULL``
#: captures the active git branch at session-create time so the resume
#: picker can render it in the meta strip and ``Ctrl+B`` can filter the
#: list to just-this-branch entries (Claude Code parity). NULL for
#: pre-v19 rows AND for sessions started outside a git repo / on a
#: detached HEAD. Backfill is impossible (we don't know history); the
#: picker renders the segment only when the value is present, so old
#: rows degrade gracefully.
#: v20 = tool_loop_trips (2026-05-16) — M1 loop-detection audit table.
#: The repetition detector records a row per trip (observe + enforce
#: mode both) so thresholds can be tuned against real data. Plain
#: append table in the F1 ``audit.db`` — replaces a per-call
#: ``CREATE TABLE IF NOT EXISTS`` that previously ran on every trip.
#: v21 = gateway_parity_log (2026-05-17) — M1 of the gateway-vs-CLI
#: intelligence-parity plan. One row per (turn, mechanism) recording
#: which of the 10 parity-affecting mechanisms fired on each gateway
#: turn. Operational telemetry feeding ``oc gateway diagnose``; plain
#: append table in ``audit.db`` (no HMAC chain, no append-only trigger
#: — mirrors v20 ``tool_loop_trips``).
#: v22 = source-rename (2026-05-18) — retag ``sessions.source`` rows from
#: the removed ``oc webui`` command's legacy ``'webui'`` label to
#: ``'workspace'`` (the surviving browser surface). Pure UPDATE of an
#: existing column; no schema-shape change.
SCHEMA_VERSION = 22

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
    cwd           TEXT,    -- Plan 3 (2026-05-01): working dir at session start,
                           -- input signal for profile_analysis_daily cwd-clusterer
    goal_text         TEXT,            -- Wave 5 (2026-05-04): /goal persistent target
    goal_active       INTEGER DEFAULT 0,
    goal_turns_used   INTEGER DEFAULT 0,
    goal_budget       INTEGER DEFAULT 20,
    goal_last_judge_reason TEXT,        -- Kanban-Goals v2 (2026-05-08): structured judge rationale
    parent_session_id TEXT,             -- delegate-lineage (2026-05-10): if this session was
                                         -- spawned by a delegate() call, this points at the
                                         -- parent's session id; NULL for root sessions.
    source            TEXT,              -- source-column (2026-05-10): origin of the row,
                                         -- one of 'cli' | 'workspace' | 'discord' | 'telegram' |
                                         -- 'slack' | 'cron' | 'tool' | 'api_server'. Used by
                                         -- the workspace sidebar to filter/group rows.
    compactions_count INTEGER DEFAULT 0, -- compactions-count (v18, 2026-05-10): number of
                                         -- times CompactionEngine rewrote this session's
                                         -- message history. Bumped by AgentLoop after
                                         -- CompactionResult.did_compact == True. Surfaced by
                                         -- /context, /usage, oc usage, oc context.
    git_branch    TEXT                   -- v19 (2026-05-11): active git branch at session
                                         -- start, captured by opencomputer.worktree.current_git_branch.
                                         -- NULL when the session started outside a git repo,
                                         -- on a detached HEAD, or for pre-v19 rows. Rendered
                                         -- in the resume-picker meta strip and used by Ctrl+B
                                         -- branch-filter.
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);

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
    tokenize='trigram'
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

-- v1.1 plan-2 M5.2 (2026-05-09): per-prompt message-history checkpoints.
-- The agent loop fires CheckpointManager.create() before each tool_use
-- block so `oc session rewind` can restore message state at a chosen
-- prior point. files_snapshot_json is NULL by default (opt-in via
-- checkpoints.snapshot_files config); messages_snapshot_json carries
-- the JSON-serialized message history at that point.
CREATE TABLE IF NOT EXISTS prompt_checkpoints (
    id                       TEXT PRIMARY KEY,
    session_id               TEXT NOT NULL,
    prompt_index             INTEGER NOT NULL,
    messages_snapshot_json   TEXT NOT NULL,
    files_snapshot_json      TEXT,
    label                    TEXT NOT NULL,
    created_at               REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_prompt_checkpoints_session
    ON prompt_checkpoints(session_id, created_at);
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
    (9, 10): "_migrate_v9_to_v10",
    (10, 11): "_migrate_v10_to_v11",
    (11, 12): "_migrate_v11_to_v12",
    (12, 13): "_migrate_v12_to_v13",
    (13, 14): "_migrate_v13_to_v14",
    (14, 15): "_migrate_v14_to_v15",
    (15, 16): "_migrate_v15_to_v16",
    (16, 17): "_migrate_v16_to_v17",
    (17, 18): "_migrate_v17_to_v18",
    (18, 19): "_migrate_v18_to_v19",
    (19, 20): "_migrate_v19_to_v20",
    (20, 21): "_migrate_v20_to_v21",
    (21, 22): "_migrate_v21_to_v22",
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


def _migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    """v0.5 of outcome-aware learning (2026-05-03).

    Adds the ``policy_audit_log`` append-only HMAC chain table for
    cryptographically protected status transitions. Closes the v0
    deferral noted in policy_audit.py: status transitions in v0 were
    UPDATEs to ``policy_changes`` (chain protected as-drafted only).
    v0.5 adds a second chain that protects every transition.

    The drafted-row chain in ``policy_changes`` stays as-is for
    backward compatibility — verify_chain on that still validates
    immutable as-drafted content. The new ``policy_audit_log`` table
    is the authoritative history of status changes.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS policy_audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            change_id   TEXT NOT NULL,
            ts          REAL NOT NULL,
            status      TEXT NOT NULL,
            actor       TEXT,
            reason      TEXT,
            hmac_prev   TEXT NOT NULL,
            hmac_self   TEXT NOT NULL,
            FOREIGN KEY (change_id) REFERENCES policy_changes(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_policy_audit_log_change
            ON policy_audit_log(change_id, ts);
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
    ("sessions", "goal_last_judge_reason", "TEXT"),  # Kanban-Goals v2 (2026-05-08)
    ("sessions", "parent_session_id", "TEXT"),  # delegate-lineage (2026-05-10)
    ("sessions", "source", "TEXT"),  # source-column (v17, 2026-05-10)
    ("sessions", "compactions_count", "INTEGER DEFAULT 0"),  # compactions-count (v18, 2026-05-10)
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


def _migrate_v10_to_v11(conn: sqlite3.Connection) -> None:
    """Hermes Wave 5 (2026-05-04) — /goal persistent cross-turn goals.

    Adds four columns to the ``sessions`` table mirroring the existing
    ``vibe`` / ``vibe_updated`` per-session-field pattern:

    - ``goal_text``       (TEXT)    — user-stated goal; NULL when no goal set
    - ``goal_active``     (INTEGER) — 0/1 paused/running; default 0
    - ``goal_turns_used`` (INTEGER) — continuation turns consumed; default 0
    - ``goal_budget``     (INTEGER) — max continuations before auto-stop; default 20

    All columns NULL/0 by default — sessions without a goal are
    indistinguishable from pre-v11 sessions.
    """
    for col, sql_type in (
        ("goal_text", "TEXT"),
        ("goal_active", "INTEGER DEFAULT 0"),
        ("goal_turns_used", "INTEGER DEFAULT 0"),
        ("goal_budget", "INTEGER DEFAULT 20"),
    ):
        try:
            conn.execute(
                f'ALTER TABLE sessions ADD COLUMN "{col}" {sql_type}'
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _migrate_v11_to_v12(conn: sqlite3.Connection) -> None:
    """Wave 6.B (2026-05-04) — trigram FTS5 tokenizer for CJK + substring search.

    Replaces the default ``porter unicode61`` tokenizer with ``trigram``.
    Trigram gives substring search out of the box AND CJK / Thai / Japanese
    matching that porter cannot. Tradeoff is a ~3× larger FTS index;
    acceptable for typical session sizes.

    Migration: drop + recreate the FTS5 virtual table with the new
    tokenizer, reindexing all existing message rows. Idempotent — safe
    to re-run.

    Fallback: if this sqlite build doesn't ship the trigram tokenizer,
    we silently fall back to porter unicode61 so the migration doesn't
    wedge.
    """
    # Check if the messages table exists. Legacy fixture DBs (e.g. ones
    # constructed by tests at v6 with sessions-only) don't have messages
    # yet — for them we just create the empty FTS table; later migration
    # steps add messages and the triggers keep it in sync.
    has_messages = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'",
    ).fetchone() is not None

    def _create_fts(tokenize: str) -> None:
        conn.executescript(
            f"""
            DROP TABLE IF EXISTS messages_fts;
            CREATE VIRTUAL TABLE messages_fts USING fts5(
                content,
                content='messages',
                content_rowid='id',
                tokenize='{tokenize}'
            );
            """
        )
        if has_messages:
            conn.execute(
                "INSERT INTO messages_fts(rowid, content) "
                "SELECT id, content FROM messages",
            )

    try:
        _create_fts("trigram")
    except sqlite3.OperationalError as exc:
        if "trigram" in str(exc).lower():
            _create_fts("porter unicode61")
        else:
            raise


def _migrate_v12_to_v13(conn: sqlite3.Connection) -> None:
    """Hermes B4 (2026-05-06) — per-LLM-call usage + cost recording.

    Adds a new ``llm_calls`` table that records one row per provider
    completion request. Distinct from ``tool_usage`` (which records tool
    dispatches): an LLM call is the agent talking to its model, a tool
    call is the agent talking to its environment.

    Schema:

    - ``provider`` / ``model`` — duplicated so cost rollups don't need
      to join messages.
    - ``input_tokens`` / ``output_tokens`` — raw counts as reported by
      the provider's usage block.
    - ``cost_usd`` — nullable: the cost-guard pricing table doesn't have
      every model, and we'd rather record None than fake zero.
    - ``batch`` — 0/1; batch API discounts factor into cost_usd.

    Indexes mirror ``tool_usage`` for symmetry.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_calls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT NOT NULL,
            ts              REAL NOT NULL,
            provider        TEXT NOT NULL,
            model           TEXT NOT NULL,
            input_tokens    INTEGER NOT NULL DEFAULT 0,
            output_tokens   INTEGER NOT NULL DEFAULT 0,
            cost_usd        REAL,
            batch           INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_llm_calls_session
            ON llm_calls(session_id);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_ts
            ON llm_calls(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_model
            ON llm_calls(model);
        """
    )


def _migrate_v13_to_v14(conn: sqlite3.Connection) -> None:
    """Kanban-Goals v2 (2026-05-08) — add ``goal_last_judge_reason``.

    Spec: docs/superpowers/specs/2026-05-08-kanban-goals-v2-design.md §6.
    Additive nullable column; old goals read NULL → ``GoalState.last_judge_reason``
    becomes ``None``. Self-heal-friendly: skip the ALTER if a column already
    exists (e.g. mixed-version rollouts).
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "goal_last_judge_reason" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN goal_last_judge_reason TEXT")


def _migrate_v14_to_v15(conn: sqlite3.Connection) -> None:
    """v1.1 plan-2 M5.2 (2026-05-09) — per-prompt message-history checkpoints.

    The DDL above already declares ``prompt_checkpoints`` with
    ``CREATE TABLE IF NOT EXISTS`` so fresh DBs get the table at v0→v1
    via the baseline DDL. Legacy DBs (v14) need an explicit create
    here so the table appears without re-running baseline DDL.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_checkpoints (
            id                       TEXT PRIMARY KEY,
            session_id               TEXT NOT NULL,
            prompt_index             INTEGER NOT NULL,
            messages_snapshot_json   TEXT NOT NULL,
            files_snapshot_json      TEXT,
            label                    TEXT NOT NULL,
            created_at               REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_prompt_checkpoints_session
            ON prompt_checkpoints(session_id, created_at)
        """
    )


def _migrate_v15_to_v16(conn: sqlite3.Connection) -> None:
    """delegate-lineage (2026-05-10) — close the audit's lineage gaps.

    Two additive changes:

    1. ``sessions.parent_session_id`` — a child session row created by
       :class:`opencomputer.tools.delegate.DelegateTool` now carries a
       schema-level link to the parent's session id. NULL = root session
       (the parent of all delegations or a CLI ``oc chat`` session).
       Indexed for ``oc sessions tree`` walks.

    2. ``subagents`` table — the in-memory ``SubagentRegistry`` is
       backed by sqlite so ``oc agents history`` / ``oc agents running``
       survive process restart. ``host_pid`` + ``host_started_at`` carry
       liveness signal; a ``running`` record whose pid is no longer
       alive (or whose pid was reused — start-time mismatch) is
       reported as ``orphaned`` at read time.

    Idempotent: the ALTER skips when the column is already present
    (fresh DBs after baseline DDL acquire it via the same migration on
    a freshly-bumped schema_version=0 path), and ``CREATE TABLE IF NOT
    EXISTS`` is naturally idempotent.
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    if "parent_session_id" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN parent_session_id TEXT"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_parent "
        "ON sessions(parent_session_id)"
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS subagents (
            agent_id          TEXT PRIMARY KEY,
            parent_session_id TEXT,
            child_session_id  TEXT,
            parent_agent_id   TEXT,
            goal              TEXT NOT NULL,
            started_at        REAL NOT NULL,
            ended_at          REAL,
            state             TEXT NOT NULL DEFAULT 'running',
            error             TEXT,
            role              TEXT NOT NULL DEFAULT 'leaf',
            agent_template    TEXT,
            isolation_mode    TEXT NOT NULL DEFAULT 'none',
            depth             INTEGER NOT NULL DEFAULT 0,
            host_pid          INTEGER NOT NULL,
            host_started_at   REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_subagents_parent_session
            ON subagents(parent_session_id);
        CREATE INDEX IF NOT EXISTS idx_subagents_child_session
            ON subagents(child_session_id);
        CREATE INDEX IF NOT EXISTS idx_subagents_state
            ON subagents(state);
        """
    )


def _migrate_v16_to_v17(conn: sqlite3.Connection) -> None:
    """source-column (2026-05-10) — add ``sessions.source`` for the workspace sidebar.

    The workspace's ``api/agent_sessions.py`` filters and groups sidebar
    rows by ``sessions.source`` (``'cli' | 'workspace' | 'discord' | ...``).
    Without the column, the helper returns ``[]`` and the sidebar can
    never show CLI-imported sessions. This migration:

      1. Adds ``sessions.source TEXT`` (NULL on add — sqlite ALTER cannot
         set NOT NULL with no default).
      2. Backfills NULL rows with ``'cli'`` since pre-v17 sessions came
         exclusively from CLI / messaging gateway flows.
      3. Indexes the new column for fast sidebar filtering.

    Idempotent: the ALTER is skipped when the column already exists.
    """
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    if "source" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT")
    conn.execute(
        "UPDATE sessions SET source = 'cli' WHERE source IS NULL OR source = ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source)"
    )


def _migrate_v17_to_v18(conn: sqlite3.Connection) -> None:
    """compactions-count (2026-05-10) — per-session compaction counter.

    Spec: ``docs/superpowers/specs/2026-05-10-cc-usage-context-visibility-design.md``.

    Adds ``sessions.compactions_count INTEGER DEFAULT 0``. Bumped by
    :class:`opencomputer.agent.loop.AgentLoop` after a successful
    :class:`CompactionEngine` rewrite (``CompactionResult.did_compact``).
    Surfaced by ``/context`` and ``/usage`` slash commands plus the new
    ``oc usage`` / ``oc context`` CLI subcommands — closes the visibility
    gaps documented in CC §4 (`/context`) and CC §10 (`/usage`).

    Idempotent: ALTER skipped when the column already exists. Pre-v18
    rows read 0 (the DEFAULT) for this column — the schema is honest
    about not having tracked the counter before this migration; we
    don't backfill historical compactions, just zero out the column
    so future bumps land on a consistent base.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "compactions_count" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN compactions_count INTEGER DEFAULT 0"
        )
    # Defensive: if a partially-migrated DB ended up with NULL values
    # (column added with no DEFAULT, e.g. pre-3.31 SQLite quirks), normalise
    # to 0 so the SUM/MAX/MIN aggregates downstream never trip.
    conn.execute(
        "UPDATE sessions SET compactions_count = 0 WHERE compactions_count IS NULL"
    )


def _migrate_v18_to_v19(conn: sqlite3.Connection) -> None:
    """git_branch (2026-05-11) — capture active git branch on session create.

    Adds ``sessions.git_branch TEXT NULL``. Populated by
    :meth:`SessionDB.ensure_session` / :meth:`SessionDB.create_session`
    when the caller passes a non-empty value (``opencomputer.worktree.current_git_branch``
    is the canonical source). Backfill of historical rows is impossible
    (we don't know what branch a months-old session was started on);
    the picker renders the segment only when the value is present, so
    NULL rows show the prior layout unchanged.

    Idempotent: ALTER is skipped when the column already exists. SQLite
    ``ADD COLUMN`` is O(1) (a schema-only change in the header) — no
    table rewrite even on multi-GB DBs.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "git_branch" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN git_branch TEXT")


def _migrate_v19_to_v20(conn: sqlite3.Connection) -> None:
    """tool_loop_trips (2026-05-16) — M1 loop-detection audit table.

    Adds the ``tool_loop_trips`` table. The repetition detector
    (:class:`opencomputer.agent.loop_safety.LoopDetector`) records a row
    here every time it trips — in both ``observe`` and ``enforce`` mode —
    so detection thresholds can be tuned against real-world data.

    The table lives in the SAME DB as the F1 ``audit_log`` chain (the
    profile's ``audit.db``, which runs this migration chain via
    :func:`apply_migrations`). Loop trips are operational telemetry, NOT
    chained security events, so they get a plain append table with no
    HMAC chain and no append-only trigger.

    Created here — once, at schema-migration time — rather than via a
    per-call ``CREATE TABLE IF NOT EXISTS`` in ``record_loop_trip``.

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` is a no-op when the table
    already exists.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tool_loop_trips (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            session_id  TEXT NOT NULL,
            depth       INTEGER NOT NULL,
            kind        TEXT NOT NULL,
            detail      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tool_loop_trips_session
            ON tool_loop_trips(session_id, ts);
        """
    )


def _migrate_v20_to_v21(conn: sqlite3.Connection) -> None:
    """gateway_parity_log (2026-05-17) — M1 gateway-vs-CLI parity telemetry.

    Adds the ``gateway_parity_log`` table. The gateway dispatcher
    (:mod:`opencomputer.gateway.parity_probe`) records one row per
    (turn, mechanism): which of the 10 parity-affecting mechanisms fired
    on each gateway turn, so ``oc gateway diagnose`` can show — and the
    M2/M3 work can prioritise — the asymmetry between CLI and gateway
    sessions.

    Like ``tool_loop_trips`` (v20) this is operational telemetry, NOT a
    chained security event, so it gets a plain append table with no HMAC
    chain and no append-only trigger. It co-exists with the F1
    ``audit_log`` chain in the same ``audit.db`` file.

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` is a no-op when the table
    already exists.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS gateway_parity_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL NOT NULL,
            session_id   TEXT NOT NULL,
            turn_id      INTEGER NOT NULL,
            platform     TEXT NOT NULL,
            mechanism_id TEXT NOT NULL,
            fired        INTEGER NOT NULL,
            detail       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gateway_parity_log_session
            ON gateway_parity_log(session_id, ts);
        CREATE INDEX IF NOT EXISTS idx_gateway_parity_log_mechanism
            ON gateway_parity_log(mechanism_id, ts);
        """
    )


def _migrate_v21_to_v22(conn: sqlite3.Connection) -> None:
    """source-rename (2026-05-18) — retag ``sessions.source`` 'webui' → 'workspace'.

    The ``oc webui`` command was removed; ``oc workspace`` is the only
    browser surface. Sessions created through the dashboard's Hermes-shape
    API were tagged ``source='webui'`` — a name that now points at nothing.
    This migration retags historical rows so the workspace sidebar groups
    them under the accurate label.

    Idempotent: a plain UPDATE that matches nothing once every row is
    already 'workspace'. The column itself is untouched — no schema-shape
    change, O(rows-matched) cost.
    """
    conn.execute(
        "UPDATE sessions SET source = 'workspace' WHERE source = 'webui'"
    )


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

    # ─── lifecycle / rebind (§9.2 profile-handoff) ──────────────────

    def close(self) -> None:
        """No-op: ``SessionDB`` opens fresh connections per operation.

        Provided for API parity with ``HandoffAuditLogger.close()`` /
        provider clients / browser harness — the rebind registry calls
        close() on subsystems that exit lifecycle. ``SessionDB`` has no
        persistent connection to close, so this is a documented no-op
        that's safe to call any number of times.
        """
        # Idempotent by design. Defensive: no exception even if called
        # after the underlying file is gone or path is unset.
        return None

    def rebind(
        self,
        new_db_path: Path,
        *,
        source_session_id: str | None = None,
        target_profile: str | None = None,
    ) -> None:
        """Re-point this SessionDB at a different sqlite file.

        Closes §9.2 of
        ``docs/plans/profile-handoff-investigation.md``: after a
        profile swap, chat history must continue in the new profile's
        ``sessions.db`` instead of stranding it in the original
        profile's.

        Optionally writes a continuation pointer row to the OLD
        database so :meth:`resume_session` / ``oc resume`` can detect
        the mid-session swap and direct the user to the new profile.

        Args:
            new_db_path: Path of the new sqlite file. Created with
                migrations applied if absent.
            source_session_id: If provided, write a continuation
                marker row into the OLD DB for this session id, so a
                later ``oc resume`` against the OLD profile can hint
                the user toward the NEW profile. Skipped when ``None``
                (e.g. before any session has been opened).
            target_profile: Display name of the target profile, used
                in the continuation marker body. Defaults to "<unknown>"
                if not supplied.

        Raises:
            TypeError: ``new_db_path`` is not a ``Path``.
        """
        import time

        if not isinstance(new_db_path, Path):
            raise TypeError(
                f"new_db_path must be a Path, got {type(new_db_path).__name__}"
            )

        # No-op when re-binding to the same file.
        if new_db_path == self.db_path:
            return

        # Continuation marker — written into the OLD DB BEFORE swap.
        # Only when a session is actually in flight (source_session_id
        # given); a fresh-process rebind has nothing to continue.
        if source_session_id:
            target_label = (target_profile or "<unknown>").strip() or "<unknown>"
            marker_body = (
                f"[profile-swap] This session continued in profile "
                f"{target_label!r} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}. "
                f"To resume, run: oc -p {target_label} chat -c {source_session_id}"
            )
            try:
                with self._txn() as conn:
                    # Ensure the session exists so the FK is satisfied —
                    # idempotent: ensure_session is no-op if it already does.
                    conn.execute(
                        "INSERT OR IGNORE INTO sessions(id, platform, model, "
                        "title, started_at) VALUES (?, ?, ?, ?, ?)",
                        (source_session_id, "cli", "", "", time.time()),
                    )
                    conn.execute(
                        "INSERT INTO messages(session_id, role, content, "
                        "timestamp) VALUES (?, ?, ?, ?)",
                        (source_session_id, "system", marker_body, time.time()),
                    )
            except Exception:  # noqa: BLE001 — best-effort marker; never block rebind
                # Log via the module logger if available; otherwise swallow.
                import logging
                logging.getLogger(__name__).warning(
                    "SessionDB.rebind: continuation marker write failed for "
                    "session=%s — rebind continues",
                    source_session_id,
                    exc_info=True,
                )

        # Swap the path. The next _connect() call goes to the new file.
        new_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = new_db_path
        # Apply migrations to the new file (idempotent if already created).
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

    def allocate_session_id(self) -> str:
        """Generate a fresh session id WITHOUT writing a DB row.

        Wave 5 T17 — Hermes-port (c5b4c4816). The companion to lazy
        session creation. Callers who want a session id immediately for
        in-memory bookkeeping (TUI / web dashboard) but who don't want
        a ghost ``sessions`` row written until the user actually sends
        a message use this + :meth:`ensure_session` instead of the eager
        :meth:`create_session`.
        """
        import uuid
        return str(uuid.uuid4())

    def ensure_session(
        self,
        session_id: str,
        *,
        platform: str = "cli",
        model: str = "",
        title: str = "",
        cwd: str | None = None,
        parent_session_id: str | None = None,
        git_branch: str | None = None,
    ) -> None:
        """Idempotent session-row insert. Existing rows are left untouched.

        Wave 5 T17 (Hermes c5b4c4816). Use after :meth:`allocate_session_id`
        on the first user message of a session — by that point we know
        the session is real, so a row should exist. Differs from
        :meth:`create_session` in that this never overwrites existing
        ``platform``/``model``/``cwd`` columns; it only inserts when no
        row is present.

        ``parent_session_id`` (delegate-lineage, 2026-05-10) records the
        delegating session's id when the row is being inserted by a
        ``DelegateTool`` invocation; defaults to ``None`` for root
        sessions. ``None`` and empty string are normalised to NULL.

        ``git_branch`` (v19, 2026-05-11) records the active git branch
        at the time of insert — sourced from
        ``opencomputer.worktree.current_git_branch(cwd)``. ``None`` and
        empty string are normalised to NULL. Branch is only meaningful
        on first insert; this method's ``ON CONFLICT DO NOTHING`` means
        an existing row's branch is never overwritten (a long-running
        session that survives a branch switch keeps its origin branch).
        """
        psid = parent_session_id or None
        branch = git_branch or None
        with self._txn() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, started_at, platform, model, title, cwd, parent_session_id, git_branch)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (session_id, time.time(), platform, model, title, cwd, psid, branch),
            )

    def create_session(
        self,
        session_id: str,
        platform: str = "cli",
        model: str = "",
        title: str = "",
        cwd: str | None = None,  # Plan 3 — captured for profile-suggester
        parent_session_id: str | None = None,  # delegate-lineage 2026-05-10
        git_branch: str | None = None,  # v19 — active git branch at create time
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

        delegate-lineage (2026-05-10): ``parent_session_id`` is the
        delegating session's id when the row is created by a
        ``DelegateTool`` call; defaults to ``None`` for root sessions.
        On UPSERT we COALESCE so a delegate-issued create_session that
        re-fires for the same id never *clears* a previously-recorded
        parent linkage, but DOES set it on first write.
        """
        psid = parent_session_id or None
        branch = git_branch or None
        with self._txn() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, started_at, platform, model, title, cwd, parent_session_id, git_branch)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  started_at        = excluded.started_at,
                  platform          = excluded.platform,
                  model             = excluded.model,
                  cwd               = excluded.cwd,
                  parent_session_id = COALESCE(
                      sessions.parent_session_id, excluded.parent_session_id
                  ),
                  git_branch        = COALESCE(sessions.git_branch, excluded.git_branch)
                """,
                (session_id, time.time(), platform, model, title, cwd, psid, branch),
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

    def list_sessions_with_preview(
        self,
        limit: int = 200,
        *,
        scope: str = "all",
        cwd: str | None = None,
        repo_paths: list[str] | None = None,
        branch_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Like :meth:`list_sessions` but joins the first user-role message.

        Returns the same dict shape as :meth:`list_sessions` plus a single
        extra key ``first_user_message`` per row. Used by the resume
        picker to render a Claude-Code-style preview line when the
        session has no title yet — so untitled rows show their actual
        conversation context instead of a useless ``default @ HH:MM``.

        Uses a correlated subquery to fetch only the first user message
        per session. Cheap enough for the picker's 200-row budget; if it
        ever becomes a hotspot, swap to a window function on a covering
        index over ``messages(session_id, role, timestamp)``.

        Phase B (2026-05-11) — Claude-Code parity scope filtering:

            ``scope="cwd"``  → ``WHERE sessions.cwd = :cwd`` (exact match).
                                Pass a non-empty ``cwd`` or this falls
                                through to no filter (and emits no rows
                                if every row has NULL cwd, which is the
                                correct outcome — "the current dir has
                                no sessions yet").
            ``scope="repo"`` → ``WHERE sessions.cwd LIKE :root || '%'``
                                for every root in ``repo_paths``. Covers
                                the main worktree + all linked worktrees
                                of the repo. ``None`` / empty list falls
                                back to ``scope="all"``.
            ``scope="all"``  → no scope filter (default, preserves the
                                pre-Phase-B behaviour).

        ``branch_filter`` is orthogonal — when set, adds
        ``AND sessions.git_branch = :branch``. NULL ``git_branch`` rows
        (pre-v19) are intentionally excluded when a branch filter is
        active. Pass ``None`` to disable.

        Defensive defaults
        -------------------
        Unknown ``scope`` values fall through to ``"all"`` rather than
        raising — keeps the picker robust against future enum drift in
        the calling layer.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if scope == "cwd" and cwd:
            clauses.append("s.cwd = ?")
            params.append(cwd)
        elif scope == "repo" and repo_paths:
            # Each worktree root contributes one ``cwd LIKE root%`` clause;
            # OR them together because a session belongs to AT MOST one
            # root. Append a trailing path separator so ``/foo`` doesn't
            # match ``/foobar``.
            like_terms = []
            for root in repo_paths:
                if not root:
                    continue
                normalized = root.rstrip("/") + "/"
                like_terms.append("s.cwd LIKE ?")
                # Match either exactly ``/path/to/repo`` OR ``/path/to/repo/...``.
                # We achieve that by OR-ing exact + prefix.
                params.append(normalized + "%")
                like_terms.append("s.cwd = ?")
                params.append(root.rstrip("/"))
            if like_terms:
                clauses.append("(" + " OR ".join(like_terms) + ")")

        if branch_filter:
            clauses.append("s.git_branch = ?")
            params.append(branch_filter)

        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT s.*,
                       (SELECT m.content
                          FROM messages m
                         WHERE m.session_id = s.id
                           AND m.role = 'user'
                         ORDER BY m.timestamp ASC
                         LIMIT 1) AS first_user_message
                  FROM sessions s
                {where_sql}
              ORDER BY s.started_at DESC
                 LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    # ─── Prompt checkpoints (CC §11 — user-invocable /checkpoint, /restore) ──

    def create_prompt_checkpoint(
        self,
        *,
        session_id: str,
        prompt_index: int,
        messages: list[dict[str, Any]],
        label: str,
        files_snapshot: dict[str, str] | None = None,
    ) -> str:
        """Write a new row to ``prompt_checkpoints`` and return its id.

        Spec: docs/OC-FROM-CLAUDE-CODE.md §11. Caller is responsible for
        determining what ``messages`` to snapshot (typically the
        in-flight message list at the user's turn boundary).

        Validation:
          - ``session_id`` must be non-empty (FK is enforced; a write
            against an unknown sid raises ``sqlite3.IntegrityError``)
          - ``label`` must be non-empty (UX contract — empty is a bug)
          - ``messages`` is JSON-encoded; non-serialisable shapes raise

        Returns the freshly-allocated uuid4 string. The id is the
        stable handle ``/restore <id>`` uses.
        """
        if not session_id:
            raise ValueError("create_prompt_checkpoint: session_id is required")
        if not label:
            raise ValueError("create_prompt_checkpoint: label is required")
        cp_id = str(uuid.uuid4())
        messages_json = json.dumps(messages)
        files_json = json.dumps(files_snapshot) if files_snapshot is not None else None
        with self._txn() as conn:
            conn.execute(
                """
                INSERT INTO prompt_checkpoints
                    (id, session_id, prompt_index, messages_snapshot_json,
                     files_snapshot_json, label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cp_id,
                    session_id,
                    int(prompt_index),
                    messages_json,
                    files_json,
                    label,
                    time.time(),
                ),
            )
        return cp_id

    def list_prompt_checkpoints(
        self, session_id: str, *, limit: int = 50
    ) -> list[PromptCheckpoint]:
        """Return checkpoints for a session, most-recent first.

        Empty / unknown session returns ``[]``. Limit clamped to
        ``[1, 1000]`` mirroring ``usage_summary_aggregate``.
        """
        if not session_id:
            return []
        clamped_limit = max(1, min(int(limit) if limit else 1, 1000))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, session_id, prompt_index,
                           messages_snapshot_json, files_snapshot_json,
                           label, created_at
                    FROM prompt_checkpoints
                    WHERE session_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (session_id, clamped_limit),
                ).fetchall()
        except sqlite3.Error as exc:
            _LOG.error(
                "list_prompt_checkpoints: SQL error for session_id=%s: %s",
                session_id,
                exc,
            )
            return []
        return [self._row_to_checkpoint(r) for r in rows if r is not None]

    def get_prompt_checkpoint(self, checkpoint_id: str) -> PromptCheckpoint | None:
        """Fetch one checkpoint by id. Returns ``None`` when:

          - id is empty / unknown
          - ``messages_snapshot_json`` is corrupt JSON (logged at
            warning; the row is treated as unreadable so ``/restore``
            falls back to listing instead of crashing)
        """
        if not checkpoint_id:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, session_id, prompt_index,
                           messages_snapshot_json, files_snapshot_json,
                           label, created_at
                    FROM prompt_checkpoints WHERE id = ?
                    """,
                    (checkpoint_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            _LOG.error("get_prompt_checkpoint: SQL error for id=%s: %s", checkpoint_id, exc)
            return None
        if row is None:
            return None
        try:
            return self._row_to_checkpoint(row)
        except (json.JSONDecodeError, TypeError) as exc:
            _LOG.warning(
                "get_prompt_checkpoint: corrupt JSON for id=%s — treating as missing: %s",
                checkpoint_id,
                exc,
            )
            return None

    def find_prompt_checkpoint_by_label(
        self, *, session_id: str, label: str
    ) -> PromptCheckpoint | None:
        """Look up a checkpoint by ``(session_id, label)``.

        Labels are non-unique; this returns the MOST RECENTLY CREATED
        row matching both. Returns ``None`` when no row matches or
        when either argument is empty.
        """
        if not session_id or not label:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, session_id, prompt_index,
                           messages_snapshot_json, files_snapshot_json,
                           label, created_at
                    FROM prompt_checkpoints
                    WHERE session_id = ? AND label = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id, label),
                ).fetchone()
        except sqlite3.Error as exc:
            _LOG.error(
                "find_prompt_checkpoint_by_label: SQL error for (%s, %s): %s",
                session_id,
                label,
                exc,
            )
            return None
        if row is None:
            return None
        try:
            return self._row_to_checkpoint(row)
        except (json.JSONDecodeError, TypeError) as exc:
            _LOG.warning(
                "find_prompt_checkpoint_by_label: corrupt JSON: %s", exc
            )
            return None

    def delete_prompt_checkpoint(self, checkpoint_id: str) -> bool:
        """Remove a checkpoint by id. Returns True if a row was
        deleted; False otherwise (no-op for unknown ids)."""
        if not checkpoint_id:
            return False
        with self._txn() as conn:
            cur = conn.execute(
                "DELETE FROM prompt_checkpoints WHERE id = ?", (checkpoint_id,)
            )
            return cur.rowcount > 0

    @staticmethod
    def _row_to_checkpoint(row: Any) -> PromptCheckpoint:
        """Decode a raw sqlite row into the typed dataclass.

        Raises ``json.JSONDecodeError`` if ``messages_snapshot_json`` is
        corrupt — callers catch and degrade to "treat as missing".
        """
        messages = json.loads(row["messages_snapshot_json"])
        if not isinstance(messages, list):
            raise json.JSONDecodeError(
                "messages_snapshot_json was not a list", row["messages_snapshot_json"], 0
            )
        files_raw = row["files_snapshot_json"]
        files_snapshot: dict[str, str] | None = None
        if files_raw:
            parsed = json.loads(files_raw)
            if isinstance(parsed, dict):
                files_snapshot = {str(k): str(v) for k, v in parsed.items()}
        return PromptCheckpoint(
            id=row["id"],
            session_id=row["session_id"],
            prompt_index=int(row["prompt_index"]),
            messages=messages,
            files_snapshot=files_snapshot,
            label=row["label"],
            created_at=float(row["created_at"]),
        )

    def find_children_sessions(
        self, parent_session_id: str
    ) -> list[dict[str, Any]]:
        """Return every session whose ``parent_session_id`` matches.

        delegate-lineage (2026-05-10). Empty parent argument returns an
        empty list — the column is nullable, and querying for "all
        roots" is the wrong semantics here (use ``list_sessions``
        instead).

        Result order is by ``started_at`` ASC so a tree walk renders
        children in the order they were spawned.
        """
        if not parent_session_id:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE parent_session_id = ? "
                "ORDER BY started_at ASC",
                (parent_session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def find_root_session(self, session_id: str, *, max_climb: int = 10) -> str:
        """Walk ``parent_session_id`` upward to find the root ancestor.

        Returns ``session_id`` itself if the row has no parent or the
        chain hits ``max_climb`` (the depth cap defends against
        accidental cycles in a corrupted DB). delegate-lineage
        (2026-05-10).
        """
        if not session_id:
            return session_id
        current = session_id
        with self._connect() as conn:
            for _ in range(max_climb):
                row = conn.execute(
                    "SELECT parent_session_id FROM sessions WHERE id = ?",
                    (current,),
                ).fetchone()
                if row is None or not row["parent_session_id"]:
                    return current
                current = row["parent_session_id"]
        return current

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

    def find_session_by_title(self, title: str) -> dict[str, Any] | None:
        """Return the session row whose ``title`` exactly matches *title*.

        Hermes-CLI parity (doc line 405) — ``oc chat --resume "title"``.
        Titles already have a unique index (NULL allowed, non-NULL
        unique), so at most one row matches. Returns ``None`` when not
        found.
        """
        with self._txn() as conn:
            cur = conn.execute(
                "SELECT * FROM sessions WHERE title = ? LIMIT 1", (title,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, row, strict=True))

    def find_sessions_by_title_lineage(self, base: str) -> list[dict[str, Any]]:
        """Return all sessions in *base*'s `name [#N]` lineage.

        Hermes-CLI parity (doc lines 442-447). ``oc chat -c "my project"``
        resolves to the latest session whose title is ``"my project"`` or
        ``"my project #2"``, ``"my project #3"``, … Rows ordered by
        ``started_at DESC`` so callers can pick row 0 as "latest".
        """
        pattern = base + " #*"
        with self._txn() as conn:
            cur = conn.execute(
                "SELECT * FROM sessions "
                "WHERE title = ? OR title GLOB ? "
                "ORDER BY started_at DESC",
                (base, pattern),
            )
            rows = cur.fetchall()
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in rows]

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
        vacuum_after_prune: bool = False,
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

        Hermes-v2: when ``vacuum_after_prune`` is True and at least one
        row was deleted, runs ``VACUUM`` to reclaim disk space (SQLite
        leaves free pages behind on plain DELETE).
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
        if deleted > 0 and vacuum_after_prune:
            # VACUUM cannot run inside an explicit transaction or with a
            # cursor open; use a fresh connection at autocommit isolation.
            with self._connect() as conn:
                conn.isolation_level = None  # autocommit
                conn.execute("VACUUM")
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

    # ─── Wave 5 (2026-05-04) — /goal persistent cross-turn goals ────────

    def set_session_goal(
        self, session_id: str, *, text: str, budget: int | None = None
    ) -> None:
        """Set or replace the goal for ``session_id``. Resets turns_used to 0.

        Mirrors hermes-agent ``265bd59c1``. ``budget`` is the maximum number of
        continuation turns the loop is allowed to inject before auto-stopping.
        When ``budget`` is ``None`` the default is read from
        :class:`opencomputer.agent.config.GoalsConfig` (Kanban-Goals v2).
        Setting a fresh goal also nulls any prior ``goal_last_judge_reason``.
        """
        if budget is None:
            from opencomputer.agent.config import default_config

            budget = default_config().goals.max_turns
        with self._txn() as conn:
            conn.execute(
                """
                UPDATE sessions
                   SET goal_text = ?, goal_active = 1,
                       goal_turns_used = 0, goal_budget = ?,
                       goal_last_judge_reason = NULL
                 WHERE id = ?
                """,
                (text, int(budget), session_id),
            )

    def get_session_goal(self, session_id: str):
        """Return :class:`opencomputer.agent.goal.GoalState` or ``None``."""
        from opencomputer.agent.goal import GoalState

        with self._txn() as conn:
            row = conn.execute(
                """
                SELECT goal_text, goal_active, goal_turns_used, goal_budget,
                       goal_last_judge_reason
                  FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return GoalState(
            text=row[0],
            active=bool(row[1]),
            turns_used=int(row[2] or 0),
            budget=int(row[3] or 20),
            last_judge_reason=row[4],
        )

    def update_session_goal(
        self,
        session_id: str,
        *,
        text: str | None = None,
        active: bool | None = None,
        turns_used: int | None = None,
        budget: int | None = None,
        last_judge_reason: str | None = None,
        clear_last_judge_reason: bool = False,
    ) -> None:
        """Patch one or more goal fields. No-op if all kwargs are None.

        ``last_judge_reason=None`` means "leave unchanged"; pass
        ``clear_last_judge_reason=True`` to explicitly null the column.
        """
        sets: list[str] = []
        params: list[object] = []
        if text is not None:
            sets.append("goal_text = ?")
            params.append(text)
        if active is not None:
            sets.append("goal_active = ?")
            params.append(1 if active else 0)
        if turns_used is not None:
            sets.append("goal_turns_used = ?")
            params.append(int(turns_used))
        if budget is not None:
            sets.append("goal_budget = ?")
            params.append(int(budget))
        if last_judge_reason is not None:
            sets.append("goal_last_judge_reason = ?")
            params.append(last_judge_reason)
        elif clear_last_judge_reason:
            sets.append("goal_last_judge_reason = NULL")
        if not sets:
            return
        params.append(session_id)
        with self._txn() as conn:
            conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params
            )

    def clear_session_goal(self, session_id: str) -> None:
        """Drop the goal — sets goal_text = NULL, goal_active = 0, turns_used = 0.

        ``goal_budget`` is preserved so a subsequent ``set_session_goal``
        without an explicit budget falls back to the default.
        ``goal_last_judge_reason`` is also nulled (a cleared goal has no
        live judge history).
        """
        with self._txn() as conn:
            conn.execute(
                """
                UPDATE sessions
                   SET goal_text = NULL, goal_active = 0, goal_turns_used = 0,
                       goal_last_judge_reason = NULL
                 WHERE id = ?
                """,
                (session_id,),
            )

    # ─── A.4 vibe thread (continued) ─────────────────────────────────

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
        # 2026-05-11: honour msg.timestamp if the producer attached one
        # (e.g. via attach_timestamps_for_pruning). Falls back to
        # time.time() so legacy producers that don't set it keep their
        # current "row is timestamped on append" semantics. This lets
        # context_pruning cache-ttl mode see the same value the DB stored.
        msg_ts = getattr(msg, "timestamp", None)
        ts_value = float(msg_ts) if msg_ts is not None else time.time()
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
            ts_value,
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

    def replace_session_messages(
        self, session_id: str, msgs: list[Message]
    ) -> list[int]:
        """Atomically replace ALL message rows for ``session_id`` with ``msgs``.

        Used by ``oc session repair`` to permanently insert synthetic
        ``<INTERRUPTED — tool result missing>`` placeholders for orphan
        ``tool_use`` blocks (the rows the wire-side
        ``reconcile_orphan_tool_calls`` synthesizes at every resume).
        Position-correct insertion is the reason this can't be a simple
        ``append`` — Anthropic requires the ``tool_result`` block to sit
        IMMEDIATELY after the matching ``tool_use``, which means the
        synthetic must land between the assistant turn and whatever
        message currently follows it. SQLite ``messages.id`` is
        ``AUTOINCREMENT`` and ordered by, so the cleanest atomic approach
        is delete-all-then-reinsert under a single transaction.

        FK safety: ``vibe_log.message_id`` is intentionally a loose FK
        (nullable INTEGER, no ``REFERENCES`` constraint) precisely so
        legacy-message cleanup like this never orphans classifier
        evidence — see the schema docstring at state.py:410. No other
        table holds an FK to ``messages.id``. Stale ``message_id``
        values in vibe_log just become NULL-equivalent for analytics.

        ``sessions.message_count`` is reset to ``len(msgs)`` to stay
        consistent with the new row count.

        Returns the list of new row IDs in insertion order. Raises
        ``ValueError`` on empty ``session_id``; an empty ``msgs`` list
        is permitted and clears the session's message rows.
        """
        if not session_id:
            raise ValueError(
                "replace_session_messages: session_id must be non-empty"
            )
        with self._txn() as conn:
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?",
                (session_id,),
            )
            ids: list[int] = []
            for msg in msgs:
                cur = conn.execute(
                    self._INSERT_MESSAGE_SQL,
                    self._msg_row(session_id, msg),
                )
                ids.append(int(cur.lastrowid or 0))
            conn.execute(
                "UPDATE sessions SET message_count = ? WHERE id = ?",
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

    def increment_compaction_count(self, session_id: str) -> int:
        """Atomically bump ``sessions.compactions_count`` for ``session_id``.

        Returns the post-increment value. Returns ``0`` (without raising)
        when:

          - ``session_id`` is empty / falsy (caller bug — logged at WARNING),
          - the session row does not exist (caller raced delete; logged
            at WARNING), or
          - the underlying SQL raised (logged at ERROR + returned 0 so the
            agent loop never wedges over a counter).

        Never raises. Compaction-counter telemetry is a "nice to have"
        for ``/context`` and ``oc usage`` — it must not break the loop.

        Spec: CC visibility design §4.2.
        """
        if not session_id or not isinstance(session_id, str):
            _LOG.warning(
                "increment_compaction_count called with empty/non-string session_id=%r; "
                "no-op",
                session_id,
            )
            return 0
        try:
            with self._txn() as conn:
                # Single-statement UPDATE-and-RETURNING gives us atomic
                # bump+read; sqlite supports RETURNING since 3.35 (2021).
                # Fall back to a second SELECT for older sqlite.
                try:
                    cur = conn.execute(
                        "UPDATE sessions SET compactions_count = "
                        "COALESCE(compactions_count, 0) + 1 "
                        "WHERE id = ? RETURNING compactions_count",
                        (session_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        _LOG.warning(
                            "increment_compaction_count: no row for session_id=%s; "
                            "counter unchanged",
                            session_id,
                        )
                        return 0
                    return int(row[0])
                except sqlite3.OperationalError as exc:
                    if "RETURNING" not in str(exc) and "syntax" not in str(exc).lower():
                        raise
                    # Fallback for SQLite < 3.35 — two-step in same txn.
                    cur = conn.execute(
                        "UPDATE sessions SET compactions_count = "
                        "COALESCE(compactions_count, 0) + 1 WHERE id = ?",
                        (session_id,),
                    )
                    if cur.rowcount == 0:
                        _LOG.warning(
                            "increment_compaction_count: no row for session_id=%s; "
                            "counter unchanged",
                            session_id,
                        )
                        return 0
                    row = conn.execute(
                        "SELECT compactions_count FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                    return int(row[0]) if row else 0
        except sqlite3.Error as exc:  # pragma: no cover — defensive belt
            _LOG.error(
                "increment_compaction_count: SQL error for session_id=%s: %s",
                session_id,
                exc,
            )
            return 0

    def session_usage_summary(self, session_id: str) -> SessionUsageRow | None:
        """Return per-session token + cache + compaction + cost totals.

        Returns ``None`` for unknown / empty session ids. Cost is summed
        from ``llm_calls`` (joined by ``session_id``); ``None`` when the
        session has no priced llm_calls rows. See CC visibility spec §4.2.
        """
        if not session_id or not isinstance(session_id, str):
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT
                        s.id, s.model, s.started_at, s.ended_at,
                        COALESCE(s.input_tokens, 0)       AS input_tokens,
                        COALESCE(s.output_tokens, 0)      AS output_tokens,
                        COALESCE(s.cache_read_tokens, 0)  AS cache_read_tokens,
                        COALESCE(s.cache_write_tokens, 0) AS cache_write_tokens,
                        COALESCE(s.compactions_count, 0)  AS compactions_count,
                        (
                            SELECT SUM(c.cost_usd)
                            FROM llm_calls c
                            WHERE c.session_id = s.id AND c.cost_usd IS NOT NULL
                        ) AS cost_usd
                    FROM sessions s
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            _LOG.error(
                "session_usage_summary: SQL error for session_id=%s: %s",
                session_id,
                exc,
            )
            return None
        if row is None:
            return None
        return SessionUsageRow(
            session_id=row["id"],
            model=row["model"] or None,
            started_at=float(row["started_at"]),
            ended_at=float(row["ended_at"]) if row["ended_at"] is not None else None,
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cache_read_tokens=int(row["cache_read_tokens"]),
            cache_write_tokens=int(row["cache_write_tokens"]),
            compactions_count=int(row["compactions_count"]),
            cost_usd=(float(row["cost_usd"]) if row["cost_usd"] is not None else None),
        )

    def usage_summary_aggregate(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        model: str | None = None,
        provider: str | None = None,
        limit: int = 50,
    ) -> list[SessionUsageRow]:
        """Return per-session usage rows for ``oc usage show``.

        Args:
            since: epoch seconds floor on ``sessions.started_at``.
            until: epoch seconds ceiling on ``sessions.started_at``.
            model: filter to sessions whose ``sessions.model`` matches
                exactly. Use ``None`` for all models.
            provider: filter to sessions where any ``llm_calls.provider``
                row matches. Implemented via ``EXISTS`` subquery so
                sessions with NO llm_calls are excluded when this filter
                is active (matches user intent: "show me my Anthropic
                sessions" means ones that actually called Anthropic).
            limit: max rows; clamped to ``[1, 1000]``. Most-recent first.

        Empty result on empty DB. Never raises; SQL errors are logged
        and surfaced as an empty list so the CLI renders an empty-state
        message instead of a stack trace.
        """
        # Input validation: clamp limit, normalise empty strings.
        clamped_limit = max(1, min(int(limit) if limit else 1, 1000))
        model_filter = model or None
        provider_filter = provider or None

        sql_parts = [
            """
            SELECT
                s.id, s.model, s.started_at, s.ended_at,
                COALESCE(s.input_tokens, 0)       AS input_tokens,
                COALESCE(s.output_tokens, 0)      AS output_tokens,
                COALESCE(s.cache_read_tokens, 0)  AS cache_read_tokens,
                COALESCE(s.cache_write_tokens, 0) AS cache_write_tokens,
                COALESCE(s.compactions_count, 0)  AS compactions_count,
                (
                    SELECT SUM(c.cost_usd)
                    FROM llm_calls c
                    WHERE c.session_id = s.id AND c.cost_usd IS NOT NULL
                ) AS cost_usd
            FROM sessions s
            """
        ]
        where: list[str] = []
        args: list[Any] = []
        if since is not None:
            where.append("s.started_at >= ?")
            args.append(float(since))
        if until is not None:
            where.append("s.started_at <= ?")
            args.append(float(until))
        if model_filter is not None:
            where.append("s.model = ?")
            args.append(model_filter)
        if provider_filter is not None:
            where.append(
                "EXISTS (SELECT 1 FROM llm_calls c WHERE c.session_id = s.id AND c.provider = ?)"
            )
            args.append(provider_filter)
        if where:
            sql_parts.append("WHERE " + " AND ".join(where))
        sql_parts.append("ORDER BY s.started_at DESC LIMIT ?")
        args.append(clamped_limit)

        sql = "\n".join(sql_parts)
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, tuple(args)).fetchall()
        except sqlite3.Error as exc:
            _LOG.error("usage_summary_aggregate: SQL error: %s", exc)
            return []
        return [
            SessionUsageRow(
                session_id=row["id"],
                model=row["model"] or None,
                started_at=float(row["started_at"]),
                ended_at=float(row["ended_at"]) if row["ended_at"] is not None else None,
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                cache_read_tokens=int(row["cache_read_tokens"]),
                cache_write_tokens=int(row["cache_write_tokens"]),
                compactions_count=int(row["compactions_count"]),
                cost_usd=(float(row["cost_usd"]) if row["cost_usd"] is not None else None),
            )
            for row in rows
        ]

    def replace_session_messages_from_checkpoint(
        self, *, session_id: str, checkpoint_id: str
    ) -> int:
        """Truncate the session's messages and replay the snapshot.

        Used by ``/restore`` (CC §11). Atomic: the truncation and the
        snapshot replay happen in one transaction. On any failure the
        session's message history is left unchanged.

        Returns the number of rows inserted. Returns ``0`` (without
        raising) when:

          - session_id or checkpoint_id is empty
          - the checkpoint does not exist
          - the checkpoint's session_id does not match (refuse to
            cross-restore — checkpoints are session-scoped)
          - the checkpoint's messages list is empty (a no-op restore
            is suspicious; refuse rather than silently nuke the session)

        Side effects:
            ``messages`` rows for ``session_id`` are deleted before
            the snapshot rows insert. The FTS5 triggers fire on both
            sides so search stays consistent.
        """
        if not session_id or not checkpoint_id:
            return 0
        cp = self.get_prompt_checkpoint(checkpoint_id)
        if cp is None:
            _LOG.warning(
                "replace_session_messages_from_checkpoint: unknown checkpoint id=%s",
                checkpoint_id,
            )
            return 0
        if cp.session_id != session_id:
            _LOG.warning(
                "replace_session_messages_from_checkpoint: refusing cross-session "
                "restore (checkpoint.session_id=%s, target=%s)",
                cp.session_id,
                session_id,
            )
            return 0
        if not cp.messages:
            _LOG.warning(
                "replace_session_messages_from_checkpoint: refusing empty-snapshot "
                "restore for session=%s (checkpoint id=%s)",
                session_id,
                checkpoint_id,
            )
            return 0

        inserted = 0
        with self._txn() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            for msg_dict in cp.messages:
                if not isinstance(msg_dict, dict):
                    continue
                role = str(msg_dict.get("role", "")) or "user"
                content = msg_dict.get("content")
                content_str = (
                    content if isinstance(content, str) else json.dumps(content)
                )
                tool_calls_raw = msg_dict.get("tool_calls")
                tool_calls_json: str | None = (
                    json.dumps(tool_calls_raw) if tool_calls_raw else None
                )
                conn.execute(
                    """
                    INSERT INTO messages(session_id, role, content, tool_call_id,
                                          tool_calls, name, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        role,
                        content_str,
                        msg_dict.get("tool_call_id"),
                        tool_calls_json,
                        msg_dict.get("name"),
                        time.time(),
                    ),
                )
                inserted += 1
        return inserted

    def get_messages(self, session_id: str) -> list[Message]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, tool_call_id, tool_calls, name, "
                "reasoning, reasoning_details, codex_reasoning_items, "
                "reasoning_replay_blocks, attachments, timestamp "
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
            # 2026-05-11: roundtrip the per-row timestamp into
            # Message.timestamp so context_pruning cache-ttl mode can see it.
            # ``r["timestamp"]`` is REAL NUMERIC in the schema; coerce
            # to float defensively (sqlite Row returns the native type
            # but stay robust against schema drift).
            try:
                raw_ts = r["timestamp"]
            except (IndexError, KeyError):
                raw_ts = None
            ts_value: float | None
            if raw_ts is None:
                ts_value = None
            else:
                try:
                    ts_value = float(raw_ts)
                except (TypeError, ValueError):
                    ts_value = None
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
                    timestamp=ts_value,
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

        Phase 2 v0: applies ``recall_penalty`` to the BM25 ranking so
        memories the policy engine has flagged as under-performing get
        suppressed in retrieval. The penalty decays exponentially over
        ~60 days back to neutral. Floor of 0.05 means penalised memories
        are never literally unreachable — the engine can't cause a
        cascade of "penalised → never cited → can never recover."
        """
        from opencomputer.agent.recall_synthesizer import (
            apply_recall_penalty,
        )

        stripped = query.strip()
        if not stripped:
            return []
        safe_q = '"' + stripped.replace('"', '""') + '"'
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.id, e.session_id, e.turn_index, e.summary, "
                "e.tools_used, e.file_paths, e.timestamp, "
                "e.recall_penalty, e.recall_penalty_updated_at, "
                "bm25(episodic_fts) AS bm25_rank "
                "FROM episodic_fts "
                "JOIN episodic_events e ON e.id = episodic_fts.rowid "
                "WHERE episodic_fts MATCH ?",
                (safe_q,),
            ).fetchall()

        # Apply recall_penalty multiplicatively. FTS5 returns NEGATIVE
        # rank values (lower = better match); we convert to magnitude
        # for the penalty math then re-sort by adjusted score.
        now = time.time()
        scored: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            penalty = d.get("recall_penalty") or 0.0
            updated = d.get("recall_penalty_updated_at")
            age_days = ((now - updated) / 86400.0) if updated else 0.0
            magnitude = abs(d.get("bm25_rank") or 0.0)
            d["adjusted_score"] = apply_recall_penalty(
                magnitude, penalty, age_days,
            )
            scored.append(d)

        # Highest adjusted_score first; ties broken by recency.
        scored.sort(
            key=lambda x: (
                -(x.get("adjusted_score") or 0.0),
                -(x.get("timestamp") or 0.0),
            ),
        )
        return scored[:limit]

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


    # ─── Hermes B4: per-LLM-call usage + cost recording ──────────────

    def record_llm_call(
        self,
        *,
        session_id: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None = None,
        batch: bool = False,
        ts: float | None = None,
    ) -> None:
        """Record one provider completion call into ``llm_calls``.

        Caller is the agent loop's post-LLM-call site, after
        ``resp.usage`` is populated. ``cost_usd`` should be computed via
        :func:`opencomputer.agent.usage_pricing.compute_call_cost` (or
        omitted, in which case it is recorded as NULL).

        Failures swallowed silently: telemetry must never wedge the loop.
        """
        try:
            with self._txn() as conn:
                conn.execute(
                    "INSERT INTO llm_calls "
                    "(session_id, ts, provider, model, input_tokens, "
                    " output_tokens, cost_usd, batch) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        ts if ts is not None else time.time(),
                        provider,
                        model,
                        int(input_tokens or 0),
                        int(output_tokens or 0),
                        cost_usd,
                        1 if batch else 0,
                    ),
                )
        except sqlite3.OperationalError:
            # Pre-v13 DB or transient lock. Drop the row rather than wedge
            # the loop. ``apply_migrations`` will catch up next session.
            pass

    def query_llm_calls(
        self,
        *,
        days: int | None = 7,
        group_by: str = "model",
    ) -> list[dict[str, Any]]:
        """Aggregate ``llm_calls`` rows for ``oc insights cost``.

        Returns rows shaped like
        ``{"key": ..., "calls": N, "input_tokens": ..., "output_tokens":
        ..., "cost_usd": Optional[float]}``.

        ``cost_usd`` is ``None`` when no row had pricing data; ``0.0``
        only when every row had pricing data summing to zero. The CLI
        renders ``None`` as ``—`` (not ``$0.00``) to keep totals honest.
        """
        col = (
            group_by
            if group_by in ("model", "provider", "session_id")
            else "model"
        )
        params: list[Any] = []
        sql = (
            f"SELECT {col} as key, "
            "COUNT(*) as calls, "
            "SUM(input_tokens) as input_tokens, "
            "SUM(output_tokens) as output_tokens, "
            "SUM(cost_usd) as cost_usd, "
            "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) as missing_cost "
            "FROM llm_calls "
        )
        if days is not None:
            sql += "WHERE ts >= ? "
            params.append(time.time() - days * 86400)
        sql += f"GROUP BY {col} ORDER BY calls DESC"

        with self._connect() as conn:
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                # Pre-v13 DB; return empty.
                return []

        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            # Promote missing_cost into a friendlier flag for callers.
            missing = int(d.pop("missing_cost") or 0)
            d["has_partial_cost"] = bool(missing) and bool(d.get("cost_usd"))
            d["all_cost_missing"] = missing == int(d.get("calls") or 0)
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
