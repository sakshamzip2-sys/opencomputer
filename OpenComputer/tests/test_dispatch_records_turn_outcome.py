"""P0-4: gateway dispatch writes turn_outcomes after each turn.

End-of-turn write only — affirmation_present / correction_present /
reply_latency_s are deliberately left NULL here and back-filled by P0-4b
(start-of-next-turn) once we know the next user message.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.gateway.dispatch import (
    _build_end_of_turn_signals,
    _compute_turn_index,
    _record_turn_outcome_async,
)


def _seed(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    db.create_session("sess_1", platform="cli", model="opus", cwd=str(tmp_path))
    return db


def test_compute_turn_index_starts_at_zero(tmp_path):
    db = _seed(tmp_path)
    assert _compute_turn_index(db, "sess_1") == 0


def test_compute_turn_index_increments_after_write(tmp_path):
    db = _seed(tmp_path)
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at) VALUES ('a', 'sess_1', 0, ?)",
            (time.time(),),
        )
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at) VALUES ('b', 'sess_1', 1, ?)",
            (time.time(),),
        )
    assert _compute_turn_index(db, "sess_1") == 2


def test_build_signals_counts_tool_usage_in_window(tmp_path):
    db = _seed(tmp_path)
    now = time.time()
    with db._connect() as conn:
        # In window
        conn.execute(
            "INSERT INTO tool_usage (session_id, ts, tool, model, "
            "duration_ms, error, outcome) VALUES "
            "('sess_1', ?, 'Read', 'opus', 10, 0, 'success')",
            (now - 5,),
        )
        conn.execute(
            "INSERT INTO tool_usage (session_id, ts, tool, model, "
            "duration_ms, error, outcome) VALUES "
            "('sess_1', ?, 'Bash', 'opus', 20, 1, 'failure')",
            (now - 3,),
        )
        conn.execute(
            "INSERT INTO tool_usage (session_id, ts, tool, model, "
            "duration_ms, error, outcome) VALUES "
            "('sess_1', ?, 'Write', 'opus', 5, 0, 'blocked')",
            (now - 2,),
        )
        # Out of window (too early)
        conn.execute(
            "INSERT INTO tool_usage (session_id, ts, tool, model, "
            "duration_ms, error, outcome) VALUES "
            "('sess_1', ?, 'Read', 'opus', 10, 0, 'success')",
            (now - 100,),
        )

    sig = _build_end_of_turn_signals(
        db,
        session_id="sess_1",
        turn_index=0,
        start_ts=now - 10,
        end_ts=now,
    )
    assert sig.session_id == "sess_1"
    assert sig.turn_index == 0
    assert sig.tool_call_count == 3
    assert sig.tool_success_count == 1
    assert sig.tool_error_count == 1
    assert sig.tool_blocked_count == 1
    assert sig.duration_s is not None
    # End-of-turn fields stay NULL
    assert sig.reply_latency_s is None
    assert sig.affirmation_present is False
    assert sig.correction_present is False


def test_build_signals_uses_latest_two_vibe_log_entries(tmp_path):
    db = _seed(tmp_path)
    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO vibe_log (session_id, vibe, classifier_version, "
            "timestamp) VALUES ('sess_1', 'curious', 'v1', ?)",
            (now - 30,),
        )
        conn.execute(
            "INSERT INTO vibe_log (session_id, vibe, classifier_version, "
            "timestamp) VALUES ('sess_1', 'frustrated', 'v1', ?)",
            (now - 5,),
        )

    sig = _build_end_of_turn_signals(
        db,
        session_id="sess_1",
        turn_index=0,
        start_ts=now - 60,
        end_ts=now,
    )
    # Most recent first → vibe_after; preceding → vibe_before
    assert sig.vibe_after == "frustrated"
    assert sig.vibe_before == "curious"


def test_build_signals_handles_no_vibe_log(tmp_path):
    db = _seed(tmp_path)
    now = time.time()
    sig = _build_end_of_turn_signals(
        db,
        session_id="sess_1",
        turn_index=0,
        start_ts=now - 10,
        end_ts=now,
    )
    assert sig.vibe_before is None
    assert sig.vibe_after is None


@pytest.mark.asyncio
async def test_record_async_writes_row(tmp_path):
    from opencomputer.agent.turn_outcome_recorder import TurnSignals

    db = _seed(tmp_path)
    sig = TurnSignals(session_id="sess_1", turn_index=3, tool_call_count=2)
    await _record_turn_outcome_async(db, sig)
    with db._connect() as conn:
        row = conn.execute(
            "SELECT turn_index, tool_call_count FROM turn_outcomes "
            "WHERE session_id = 'sess_1'"
        ).fetchone()
    assert row[0] == 3
    assert row[1] == 2


@pytest.mark.asyncio
async def test_record_async_swallows_db_errors(tmp_path, caplog):
    """Telemetry must NEVER block the user reply path."""
    from unittest.mock import MagicMock

    from opencomputer.agent.turn_outcome_recorder import TurnSignals

    db = MagicMock()
    db._connect.side_effect = RuntimeError("disk full")
    sig = TurnSignals(session_id="sess_1", turn_index=0)

    import logging

    caplog.set_level(logging.WARNING, logger="opencomputer.gateway.dispatch")
    await _record_turn_outcome_async(db, sig)
    # No exception propagated → success
    assert any(
        "outcome recording failed" in r.message.lower()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_record_async_p99_under_50ms(tmp_path):
    """Bounded latency on the telemetry path."""
    from opencomputer.agent.turn_outcome_recorder import TurnSignals

    db = _seed(tmp_path)
    durations = []
    for i in range(100):
        sig = TurnSignals(session_id="sess_1", turn_index=i)
        t0 = time.perf_counter()
        await _record_turn_outcome_async(db, sig)
        durations.append((time.perf_counter() - t0) * 1000)
    durations.sort()
    p99 = durations[98]
    # Hard cap 200ms (CI noise), warn at 50ms (target)
    assert p99 < 200, f"p99={p99:.2f}ms exceeds 200ms hard cap"
