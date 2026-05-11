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


def test_list_sessions_with_preview_returns_first_user_message(tmp_path: Path) -> None:
    """JOIN variant returns the first user-role message per session.

    Powers the resume picker's Claude-Code-style preview line for
    untitled sessions. Verifies: (a) the field is named
    ``first_user_message``, (b) it picks the *first* user message by
    timestamp, (c) it picks only user-role messages (skips assistant),
    (d) NULL when no user message exists yet.
    """
    from plugin_sdk.core import Message

    db = SessionDB(tmp_path / "test.db")
    db.create_session(session_id="sid-1", platform="cli", model="m", cwd="/x")
    db.create_session(session_id="sid-2", platform="cli", model="m", cwd="/y")
    db.create_session(session_id="sid-3", platform="cli", model="m", cwd="/z")

    # sid-1: user-then-assistant; preview should be the user msg
    db.append_message("sid-1", Message(role="user", content="first user prompt"))
    db.append_message(
        "sid-1", Message(role="assistant", content="my assistant reply")
    )
    db.append_message("sid-1", Message(role="user", content="second user prompt"))

    # sid-2: assistant-only (rare but possible) — first_user_message is None
    db.append_message("sid-2", Message(role="assistant", content="hi"))

    # sid-3: no messages at all
    # (no append)

    rows = db.list_sessions_with_preview(limit=10)
    by_id = {r["id"]: r for r in rows}

    assert "first_user_message" in by_id["sid-1"]
    assert by_id["sid-1"]["first_user_message"] == "first user prompt"
    assert by_id["sid-2"]["first_user_message"] is None
    assert by_id["sid-3"]["first_user_message"] is None


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
