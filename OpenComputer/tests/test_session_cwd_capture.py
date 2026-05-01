"""Plan 3 of 3 — Session cwd capture for profile-suggester pattern detection.

Plan adapted on execution: the actual API is ``create_session``, not
``record_session_start``, and ``list_sessions`` returns ``list[dict]``
not a dataclass — both verified against current `agent/state.py`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from opencomputer.agent.state import SessionDB


def test_session_cwd_persisted(tmp_path: Path) -> None:
    """create_session writes cwd; list_sessions returns it."""
    db = SessionDB(tmp_path / "test.db")
    db.create_session(
        session_id="sid-1",
        platform="cli",
        model="test-model",
        cwd="/Users/test/Vscode/work",
    )
    rows = db.list_sessions(limit=10)
    assert len(rows) == 1
    assert rows[0]["cwd"] == "/Users/test/Vscode/work"


def test_session_cwd_optional_for_legacy_rows(tmp_path: Path) -> None:
    """Old rows (pre-migration) have NULL cwd; reads return None.

    Self-heal in `agent/state.py:_self_heal_columns` re-asserts the
    cwd column on every connect via ``_EXPECTED_COLUMNS``, so legacy
    DBs auto-upgrade without needing a schema_version bump.
    """
    db_path = tmp_path / "legacy.db"
    # Simulate a pre-Plan-3 DB with no cwd column
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (6);
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
            platform TEXT NOT NULL, model TEXT, title TEXT,
            message_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0, vibe TEXT, vibe_updated REAL
        );
        INSERT INTO sessions (id, started_at, platform, model)
        VALUES ('legacy-1', 1000.0, 'cli', 'old-model');
    """)
    conn.commit()
    conn.close()

    db = SessionDB(db_path)  # _self_heal_columns adds cwd column on connect
    rows = db.list_sessions(limit=10)
    assert len(rows) == 1
    assert rows[0]["cwd"] is None  # Legacy row has NULL cwd


def test_session_cwd_default_none_for_callers_without_kwarg(tmp_path: Path) -> None:
    """Backwards compat: callers that don't pass cwd get NULL stored."""
    db = SessionDB(tmp_path / "test.db")
    db.create_session(session_id="sid-1", platform="cli", model="m")
    rows = db.list_sessions(limit=10)
    assert len(rows) == 1
    assert rows[0]["cwd"] is None
