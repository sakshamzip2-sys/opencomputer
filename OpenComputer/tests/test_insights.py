"""Tier-A item 11 — schema v5 + tool_usage table + insights CLI."""

from __future__ import annotations

import time

import pytest
from typer.testing import CliRunner

from opencomputer.agent.state import SCHEMA_VERSION, SessionDB

runner = CliRunner()


# ──────────────────────────── schema v5 ────────────────────────────


def test_schema_version_advanced_to_5():
    """Sanity-check the constant matches what we ship."""
    assert SCHEMA_VERSION >= 5


def test_fresh_db_has_tool_usage_table(tmp_path):
    db = SessionDB(tmp_path / "sessions.db")
    # If the table is missing, query_tool_usage swallows the error and
    # returns []. Direct schema introspection makes the assertion precise.
    import sqlite3
    with sqlite3.connect(tmp_path / "sessions.db") as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_usage'"
        )
        assert cur.fetchone() is not None


def test_legacy_v4_db_migrates_cleanly(tmp_path):
    """An older v4 DB should advance to v5 + acquire the new table."""
    import sqlite3
    db_path = tmp_path / "sessions.db"
    # Hand-build a minimal v4 DB shape
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (4);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, started_at REAL NOT NULL,
                ended_at REAL, platform TEXT NOT NULL,
                model TEXT, title TEXT, message_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, tool_call_id TEXT, tool_calls TEXT,
                name TEXT, reasoning TEXT, reasoning_details TEXT,
                codex_reasoning_items TEXT, timestamp REAL NOT NULL
            );
            """
        )
    # Open via SessionDB — should run the v4→v5 migration on construction.
    SessionDB(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("SELECT version FROM schema_version")
        assert cur.fetchone()[0] >= 5
        # tool_usage should exist now.
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_usage'"
        )
        assert cur.fetchone() is not None


# ──────────────────────────── record + query ────────────────────────────


def _make_session(db: SessionDB, sid: str = "s1", platform: str = "cli") -> str:
    db.create_session(session_id=sid, platform=platform, model="x")
    return sid


def test_record_tool_usage_round_trip(tmp_path):
    db = SessionDB(tmp_path / "sessions.db")
    sid = _make_session(db)

    db.record_tool_usage(
        session_id=sid, tool="Read",
        outcome="success", duration_ms=12.3, model="claude-sonnet-4-7",
    )
    db.record_tool_usage(
        session_id=sid, tool="Read",
        outcome="failure", duration_ms=400.0, model="claude-sonnet-4-7",
    )
    db.record_tool_usage(
        session_id=sid, tool="Bash",
        outcome="success", duration_ms=1200.0, model="claude-sonnet-4-7",
    )

    rows = db.query_tool_usage(days=30, group_by="tool")
    by_key = {r["key"]: r for r in rows}
    assert "Read" in by_key
    assert by_key["Read"]["calls"] == 2
    assert by_key["Read"]["errors"] == 1
    assert by_key["Read"]["error_rate"] == pytest.approx(0.5)
    assert "Bash" in by_key
    assert by_key["Bash"]["calls"] == 1
    assert by_key["Bash"]["errors"] == 0


def test_query_respects_days_window(tmp_path):
    db = SessionDB(tmp_path / "sessions.db")
    sid = _make_session(db)
    # Record one row 10 days ago.
    db.record_tool_usage(
        session_id=sid, tool="Old",
        outcome="success", duration_ms=1.0, ts=time.time() - 10 * 86400,
    )
    # And one today.
    db.record_tool_usage(
        session_id=sid, tool="New", outcome="success", duration_ms=1.0,
    )
    # 7-day window: only New.
    rows = db.query_tool_usage(days=7)
    keys = {r["key"] for r in rows}
    assert "New" in keys
    assert "Old" not in keys
    # All-time: both.
    rows = db.query_tool_usage(days=None)
    keys = {r["key"] for r in rows}
    assert "Old" in keys
    assert "New" in keys


def test_query_group_by_model(tmp_path):
    db = SessionDB(tmp_path / "sessions.db")
    sid = _make_session(db)
    db.record_tool_usage(
        session_id=sid, tool="Read", outcome="success", duration_ms=1, model="haiku",
    )
    db.record_tool_usage(
        session_id=sid, tool="Read", outcome="success", duration_ms=1, model="haiku",
    )
    db.record_tool_usage(
        session_id=sid, tool="Read", outcome="success", duration_ms=1, model="sonnet",
    )

    rows = db.query_tool_usage(days=30, group_by="model")
    by_key = {r["key"]: r for r in rows}
    assert by_key["haiku"]["calls"] == 2
    assert by_key["sonnet"]["calls"] == 1


def test_record_swallows_pre_v5_db_error(tmp_path):
    """Older DB without the table — record_tool_usage must not raise."""
    import sqlite3
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    sid = _make_session(db)
    # Drop the table to simulate a corrupt / partial-migration state.
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE tool_usage")
    # No raise — silent drop is the contract.
    db.record_tool_usage(
        session_id=sid, tool="Read", outcome="success", duration_ms=1.0,
    )


# ──────────────────────────── CLI ────────────────────────────


def test_insights_cli_no_db_handles_gracefully(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.cli import app
    result = runner.invoke(app, ["insights"])
    assert result.exit_code == 0
    assert "No sessions.db" in result.stdout


def test_insights_cli_empty_table_message(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "sessions.db")
    from opencomputer.cli import app
    result = runner.invoke(app, ["insights"])
    assert result.exit_code == 0
    assert "No tool_usage rows" in result.stdout


def test_insights_cli_renders_table(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "sessions.db")
    sid = _make_session(db)
    db.record_tool_usage(
        session_id=sid, tool="WebSearch",
        outcome="success", duration_ms=125.0, model="sonnet",
    )
    db.record_tool_usage(
        session_id=sid, tool="WebSearch",
        outcome="failure", duration_ms=2500.0, model="sonnet",
    )
    db.record_tool_usage(
        session_id=sid, tool="Read",
        outcome="success", duration_ms=4.5, model="sonnet",
    )

    from opencomputer.cli import app
    result = runner.invoke(app, ["insights", "--days", "30"])
    assert result.exit_code == 0
    assert "WebSearch" in result.stdout
    assert "Read" in result.stdout
    # WebSearch had 1 error of 2 calls = 50%
    assert "50.0%" in result.stdout


def test_insights_cli_group_by_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "sessions.db")
    sid = _make_session(db)
    db.record_tool_usage(
        session_id=sid, tool="X", outcome="success", duration_ms=1.0, model="haiku",
    )
    from opencomputer.cli import app
    result = runner.invoke(app, ["insights", "--by", "model"])
    assert result.exit_code == 0
    assert "haiku" in result.stdout
