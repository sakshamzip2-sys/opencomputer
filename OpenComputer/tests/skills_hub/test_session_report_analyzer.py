"""Tests for the session-report analyzer (opencomputer/skills/session-report/analyze_sessions.py).

Builds a synthetic SessionDB-shaped sqlite DB with a couple of sessions
+ messages and verifies the analyzer rolls them up into the expected
JSON shape. We test the helper functions directly — running the CLI
end-to-end requires the ``~/.opencomputer/`` directory which isn't
appropriate for unit tests.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# Load the analyzer module by file path — it lives under
# opencomputer/skills/session-report/ which isn't an importable package.
ANALYZER_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "opencomputer"
    / "skills"
    / "session-report"
    / "analyze_sessions.py"
)
spec = importlib.util.spec_from_file_location(
    "analyze_sessions", ANALYZER_PATH
)
assert spec and spec.loader
analyze_sessions = importlib.util.module_from_spec(spec)
sys.modules["analyze_sessions"] = analyze_sessions
spec.loader.exec_module(analyze_sessions)


def _build_synthetic_db(path: Path) -> None:
    """Create a minimum-viable SessionDB-shaped DB for the queries used."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
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
            source            TEXT,
            compactions_count INTEGER DEFAULT 0,
            git_branch        TEXT
        );
        CREATE TABLE messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            timestamp   REAL NOT NULL
        );
        """
    )
    now = time.time()
    rows = [
        # (id, started_at, ended_at, platform, model, title, msgs,
        #  in, out, cache_r, cache_w, source, compactions, git_branch)
        ("s1", now - 100, now - 50, "cli", "claude", "first session",
         5, 50_000, 2_000, 30_000, 5_000, "cli", 0, "main"),
        ("s2", now - 200, now - 150, "webui", "claude", "second session",
         3, 200_000, 8_000, 0, 0, "webui", 1, "main"),
    ]
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES (?,?,?,?)",
        [
            ("s1", "user", "Short prompt", now - 99),
            ("s1", "user", "A " * 500, now - 95),  # long → top prompt
            ("s2", "user", "Webui prompt", now - 199),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def synthetic_db(tmp_path: Path) -> Path:
    db = tmp_path / "sessions.db"
    _build_synthetic_db(db)
    return db


def test_parse_since_relative():
    assert analyze_sessions.parse_since("7d") is not None
    assert analyze_sessions.parse_since("24h") is not None
    assert analyze_sessions.parse_since("all") is None
    assert analyze_sessions.parse_since(None) is None


def test_parse_since_invalid():
    assert analyze_sessions.parse_since("garbage") is None
    assert analyze_sessions.parse_since("7x") is None


def test_query_sessions_returns_all(synthetic_db):
    conn = analyze_sessions._connect(synthetic_db)
    sessions = analyze_sessions.query_sessions(conn, since=None)
    assert len(sessions) == 2


def test_query_sessions_honours_since(synthetic_db):
    conn = analyze_sessions._connect(synthetic_db)
    # Cut-off in the future of session 2 (started 200s ago) and past of
    # session 1 (started 100s ago) → only s1 returned.
    cutoff = time.time() - 150
    sessions = analyze_sessions.query_sessions(conn, since=cutoff)
    assert {s["id"] for s in sessions} == {"s1"}


def test_query_top_prompts_orders_by_length(synthetic_db):
    conn = analyze_sessions._connect(synthetic_db)
    top = analyze_sessions.query_top_prompts(conn, since=None, n=2)
    assert len(top) == 2
    # Longest first.
    assert top[0]["chars"] >= top[1]["chars"]


def test_aggregate_rolls_per_source(synthetic_db):
    conn = analyze_sessions._connect(synthetic_db)
    sessions = analyze_sessions.query_sessions(conn, since=None)
    agg = analyze_sessions.aggregate(sessions, cache_break_threshold=100_000)
    by_project = agg["by_project"]
    assert "cli" in by_project
    assert "webui" in by_project
    assert by_project["cli"]["sessions"] == 1
    assert by_project["webui"]["sessions"] == 1
    # s2 has 200k uncached input → counts as a cache break
    assert by_project["webui"]["cache_breaks_over_100k"] == 1
    assert by_project["cli"]["cache_breaks_over_100k"] == 0


def test_aggregate_overall_totals(synthetic_db):
    conn = analyze_sessions._connect(synthetic_db)
    sessions = analyze_sessions.query_sessions(conn, since=None)
    agg = analyze_sessions.aggregate(sessions, cache_break_threshold=100_000)
    overall = agg["overall"]
    assert overall["sessions"] == 2
    assert overall["input_tokens"]["uncached"] == 250_000
    assert overall["output_tokens"] == 10_000
    assert overall["cache_breaks_over_100k"] == 1
    assert overall["compactions"] == 1


def test_subagents_query_handles_missing_table(synthetic_db):
    # The synthetic DB has no `subagents` table — query should return {}.
    conn = analyze_sessions._connect(synthetic_db)
    assert analyze_sessions.query_subagents(conn, since=None) == {}


def test_skill_usage_query_handles_missing_table(synthetic_db):
    conn = analyze_sessions._connect(synthetic_db)
    assert analyze_sessions.query_skill_usage(conn, since=None) == {}
