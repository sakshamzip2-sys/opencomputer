"""P0-5: turn_outcomes_sweep cron jobs (self-cancel + abandonment)."""
from __future__ import annotations

import time

from opencomputer.agent.state import SessionDB
from opencomputer.cron.turn_outcomes_sweep import (
    sweep_abandonments,
    sweep_self_cancels,
)


def _seed_session(db, sid="s1"):
    db.create_session(sid, platform="cli", model="opus", cwd="/tmp")
    return sid


def test_sweep_self_cancels_detects_write_then_bash_within_window(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    sid = _seed_session(db)

    now = time.time()
    with db._connect() as conn:
        # Insert turn_outcomes spanning the window
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at, self_cancel_count) VALUES "
            "(?, ?, 0, ?, 0)",
            ("to1", sid, now - 30),
        )
        conn.execute(
            "INSERT INTO tool_usage (session_id, ts, tool, model, duration_ms, "
            "error, outcome) VALUES (?, ?, 'Write', 'opus', 50, 0, 'success')",
            (sid, now - 50),
        )
        conn.execute(
            "INSERT INTO tool_usage (session_id, ts, tool, model, duration_ms, "
            "error, outcome) VALUES (?, ?, 'Bash', 'opus', 50, 0, 'success')",
            (sid, now - 30),  # 20s after Write — within 60s window
        )

    n = sweep_self_cancels(db, since_ts=now - 600)
    assert n >= 1

    with db._connect() as conn:
        cnt = conn.execute(
            "SELECT self_cancel_count FROM turn_outcomes WHERE id = 'to1'"
        ).fetchone()[0]
    assert cnt >= 1


def test_sweep_self_cancels_ignores_pairs_outside_window(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    sid = _seed_session(db)

    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at, self_cancel_count) VALUES "
            "(?, ?, 0, ?, 0)",
            ("to1", sid, now - 1000),
        )
        conn.execute(
            "INSERT INTO tool_usage (session_id, ts, tool, model, duration_ms, "
            "error, outcome) VALUES (?, ?, 'Write', 'opus', 50, 0, 'success')",
            (sid, now - 1000),
        )
        conn.execute(
            "INSERT INTO tool_usage (session_id, ts, tool, model, duration_ms, "
            "error, outcome) VALUES (?, ?, 'Bash', 'opus', 50, 0, 'success')",
            (sid, now - 800),  # 200s after — outside 60s window
        )

    sweep_self_cancels(db, since_ts=now - 2000)

    with db._connect() as conn:
        cnt = conn.execute(
            "SELECT self_cancel_count FROM turn_outcomes WHERE id = 'to1'"
        ).fetchone()[0]
    assert cnt == 0


def test_sweep_abandonments_marks_inactive_sessions(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    sid = _seed_session(db)

    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at, conversation_abandoned) VALUES "
            "(?, ?, 0, ?, 0)",
            ("to1", sid, now - 90000),  # 25 hr ago
        )

    n = sweep_abandonments(db, threshold_s=86400)
    assert n == 1

    with db._connect() as conn:
        flag = conn.execute(
            "SELECT conversation_abandoned FROM turn_outcomes WHERE id = 'to1'"
        ).fetchone()[0]
    assert flag == 1


def test_sweep_abandonments_skips_recent_sessions(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    sid = _seed_session(db)

    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at, conversation_abandoned) VALUES "
            "(?, ?, 0, ?, 0)",
            ("to1", sid, now - 3600),  # 1h ago — well within threshold
        )

    n = sweep_abandonments(db, threshold_s=86400)
    assert n == 0


def test_sweep_abandonments_only_marks_last_turn_per_session(tmp_path):
    """If a session has multiple turn_outcomes rows, only the LAST one
    is the candidate for 'abandoned'. Earlier turns had follow-ups."""
    db = SessionDB(tmp_path / "s.db")
    sid = _seed_session(db)

    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at, conversation_abandoned) VALUES "
            "('first', ?, 0, ?, 0)",
            (sid, now - 100000),
        )
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at, conversation_abandoned) VALUES "
            "('last', ?, 1, ?, 0)",
            (sid, now - 90000),
        )

    sweep_abandonments(db, threshold_s=86400)

    with db._connect() as conn:
        first = conn.execute(
            "SELECT conversation_abandoned FROM turn_outcomes WHERE id='first'"
        ).fetchone()[0]
        last = conn.execute(
            "SELECT conversation_abandoned FROM turn_outcomes WHERE id='last'"
        ).fetchone()[0]

    assert first == 0  # earlier turn — had a follow-up, not abandoned
    assert last == 1   # last turn — no follow-up, abandoned
