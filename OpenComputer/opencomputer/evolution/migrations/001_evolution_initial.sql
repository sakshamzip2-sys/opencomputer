-- Evolution storage initial schema (version 1).
-- Applied by opencomputer.evolution.storage.apply_pending().
-- Design reference: OpenComputer/docs/evolution/design.md §4.4.

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trajectory_records (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            TEXT NOT NULL,
    record_schema_version INTEGER NOT NULL,
    started_at            REAL NOT NULL,
    ended_at              REAL,
    completion_flag       INTEGER NOT NULL DEFAULT 0,
    reward_score          REAL,
    created_at            REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traj_session  ON trajectory_records(session_id);
CREATE INDEX IF NOT EXISTS idx_traj_ended_at ON trajectory_records(ended_at);

CREATE TABLE IF NOT EXISTS trajectory_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id     INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    message_id    INTEGER,
    action_type   TEXT NOT NULL,
    tool_name     TEXT,
    outcome       TEXT NOT NULL,
    timestamp     REAL NOT NULL,
    metadata_json TEXT,
    FOREIGN KEY (record_id) REFERENCES trajectory_records(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_record ON trajectory_events(record_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_tool   ON trajectory_events(tool_name);
