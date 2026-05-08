"""Schema v14 migration + last_judge_reason CRUD round-trip.

Spec: docs/superpowers/specs/2026-05-08-kanban-goals-v2-design.md §6.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from opencomputer.agent.state import SessionDB


def test_migration_v13_to_v14_adds_goal_last_judge_reason(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    # Build a minimal v13-shaped DB by stamping the version explicitly,
    # creating only the sessions table (missing the new column).
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            goal_text TEXT, goal_active INTEGER DEFAULT 0,
            goal_turns_used INTEGER DEFAULT 0, goal_budget INTEGER DEFAULT 20
        );
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (13);
        """
    )
    conn.commit()
    conn.close()

    # Opening through SessionDB triggers apply_migrations.
    SessionDB(db_path)

    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    assert "goal_last_judge_reason" in cols
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version >= 14
    conn.close()


def test_migration_v13_to_v14_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    SessionDB(db_path)  # creates fresh
    SessionDB(db_path)  # opens again — must not error
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    assert "goal_last_judge_reason" in cols
    conn.close()


def test_last_judge_reason_round_trip(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_test"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)
    db.set_session_goal(sid, text="ship it", budget=20)
    db.update_session_goal(sid, last_judge_reason="halfway done")

    g = db.get_session_goal(sid)
    assert g is not None
    assert g.last_judge_reason == "halfway done"


def test_last_judge_reason_defaults_none(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_test"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)
    db.set_session_goal(sid, text="ship it", budget=20)
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.last_judge_reason is None


def test_set_session_goal_clears_last_reason(tmp_path: Path) -> None:
    """Setting a fresh goal nulls any previous reason — fresh slate."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_test"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)
    db.set_session_goal(sid, text="first", budget=20)
    db.update_session_goal(sid, last_judge_reason="some progress")
    db.set_session_goal(sid, text="second", budget=20)

    g = db.get_session_goal(sid)
    assert g is not None
    assert g.text == "second"
    assert g.last_judge_reason is None


def test_clear_session_goal_nulls_reason(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_test"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)
    db.set_session_goal(sid, text="x", budget=20)
    db.update_session_goal(sid, last_judge_reason="something")
    db.clear_session_goal(sid)

    # Re-set so the goal row is present again with a fresh budget.
    db.set_session_goal(sid, text="y", budget=20)
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.last_judge_reason is None
