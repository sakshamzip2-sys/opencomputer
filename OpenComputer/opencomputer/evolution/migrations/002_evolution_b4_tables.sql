-- Evolution B4 schema additions (version 2).
-- Applied by opencomputer.evolution.storage.apply_pending().
-- Adds reflections, skill_invocations, and prompt_proposals tables.
-- Design reference: OpenComputer/docs/evolution/design.md §11.

CREATE TABLE IF NOT EXISTS reflections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoked_at      REAL NOT NULL,
    window_size     INTEGER NOT NULL,
    records_count   INTEGER NOT NULL,
    insights_count  INTEGER NOT NULL,
    records_hash    TEXT NOT NULL,             -- sha256 of trajectory ids in window
    cache_hit       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_reflections_invoked_at ON reflections(invoked_at);

CREATE TABLE IF NOT EXISTS skill_invocations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL,
    invoked_at   REAL NOT NULL,
    source       TEXT NOT NULL DEFAULT 'manual'  -- "manual" | "agent_loop" | "cli_promote"
);

CREATE INDEX IF NOT EXISTS idx_skill_invocations_slug ON skill_invocations(slug);
CREATE INDEX IF NOT EXISTS idx_skill_invocations_invoked_at ON skill_invocations(invoked_at);

CREATE TABLE IF NOT EXISTS prompt_proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_at   REAL NOT NULL,
    target        TEXT NOT NULL,                -- "system" | "tool_spec"
    diff_hint     TEXT NOT NULL,                -- natural-language diff description
    insight_json  TEXT NOT NULL,                -- full Insight serialized for replay
    status        TEXT NOT NULL DEFAULT 'pending', -- "pending" | "applied" | "rejected"
    decided_at    REAL,
    decided_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_prompt_proposals_status ON prompt_proposals(status);
