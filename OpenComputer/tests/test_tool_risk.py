"""v0.5 Item: tool-risk dashboard."""
from __future__ import annotations

import time

from opencomputer.agent.state import SessionDB
from opencomputer.evolution.tool_risk import compute_tool_risk


def _seed_tool_call(db, sid, tool, ts, error=0, outcome="success"):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO tool_usage (session_id, ts, tool, model, "
            "duration_ms, error, outcome) VALUES (?, ?, ?, 'opus', "
            "10.0, ?, ?)",
            (sid, ts, tool, error, outcome),
        )


def test_empty_db_returns_empty_list(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    rows = compute_tool_risk(db)
    assert rows == []


def test_error_rate_computed(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s1", platform="cli", model="m")
    now = time.time()
    # 4 calls of "Bash": 1 error
    _seed_tool_call(db, "s1", "Bash", now, error=0)
    _seed_tool_call(db, "s1", "Bash", now, error=0)
    _seed_tool_call(db, "s1", "Bash", now, error=0)
    _seed_tool_call(db, "s1", "Bash", now, error=1, outcome="failure")

    rows = compute_tool_risk(db)
    bash = next(r for r in rows if r.tool == "Bash")
    assert bash.n_calls == 4
    assert abs(bash.error_rate - 0.25) < 1e-9


def test_self_cancel_rate_computed(tmp_path):
    """Tool calls associated with turn_outcomes that have
    self_cancel_count > 0 raise the tool's self_cancel_rate."""
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s1", platform="cli", model="m")
    now = time.time()

    # Insert a turn_outcomes row that says "yes there was a self-cancel
    # near this time"
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at, self_cancel_count) VALUES (?, ?, 0, ?, 1)",
            ("to1", "s1", now),
        )

    # Two Write calls — one near the self-cancel turn, one far away
    _seed_tool_call(db, "s1", "Write", now)  # near (within 60s)
    _seed_tool_call(db, "s1", "Write", now - 3600)  # far

    rows = compute_tool_risk(db, days=7)
    write = next(r for r in rows if r.tool == "Write")
    assert write.n_calls == 2
    assert 0.0 < write.self_cancel_rate < 1.0


def test_old_calls_excluded_by_window(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s1", platform="cli", model="m")
    now = time.time()
    _seed_tool_call(db, "s1", "Read", now - 8 * 86400)  # outside 7-day window

    rows = compute_tool_risk(db, days=7)
    assert rows == []
