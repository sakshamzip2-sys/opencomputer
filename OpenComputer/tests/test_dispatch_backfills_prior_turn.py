"""P0-4b: at start-of-next-turn, back-fill prior turn_outcomes row with
affirmation/correction/latency derived from the new user message.

Companion to P0-4 — that task writes the row at end-of-turn with those
columns NULL/0; this task fills them in on the next user message.
"""
from __future__ import annotations

import time

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.gateway.dispatch import _backfill_prior_turn_async


def _seed(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    db.create_session("sess_1", platform="cli", model="opus", cwd=str(tmp_path))
    return db


def _insert_pending_row(db, session_id, turn_index, created_at):
    """Insert a 'just-finished, awaiting back-fill' turn_outcomes row."""
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at, affirmation_present, correction_present, "
            "reply_latency_s) VALUES (?, ?, ?, ?, 0, 0, NULL)",
            (f"to_{turn_index}", session_id, turn_index, created_at),
        )


@pytest.mark.asyncio
async def test_backfill_sets_affirmation_and_latency(tmp_path):
    db = _seed(tmp_path)
    now = time.time()
    _insert_pending_row(db, "sess_1", 0, now - 12.5)

    await _backfill_prior_turn_async(
        db, session_id="sess_1", user_text="thanks!", now_ts=now,
    )

    with db._connect() as conn:
        row = conn.execute(
            "SELECT affirmation_present, correction_present, reply_latency_s "
            "FROM turn_outcomes WHERE id = 'to_0'"
        ).fetchone()
    assert row[0] == 1
    assert row[1] == 0
    assert abs(row[2] - 12.5) < 0.5  # within wallclock tolerance


@pytest.mark.asyncio
async def test_backfill_sets_correction(tmp_path):
    db = _seed(tmp_path)
    now = time.time()
    _insert_pending_row(db, "sess_1", 0, now - 5.0)

    await _backfill_prior_turn_async(
        db, session_id="sess_1", user_text="no that's wrong", now_ts=now,
    )

    with db._connect() as conn:
        row = conn.execute(
            "SELECT affirmation_present, correction_present FROM turn_outcomes "
            "WHERE id = 'to_0'"
        ).fetchone()
    assert row[0] == 0
    assert row[1] == 1


@pytest.mark.asyncio
async def test_backfill_no_prior_turn_is_noop(tmp_path):
    """First message of a session — no prior row to back-fill."""
    db = _seed(tmp_path)
    # No turn_outcomes row inserted
    await _backfill_prior_turn_async(
        db, session_id="sess_1", user_text="hi", now_ts=time.time(),
    )
    with db._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM turn_outcomes WHERE session_id = 'sess_1'"
        ).fetchone()[0]
    assert n == 0  # nothing created, nothing modified


@pytest.mark.asyncio
async def test_backfill_only_updates_pending_rows(tmp_path):
    """Once a row has been back-filled (reply_latency_s NOT NULL), the
    next user message must NOT re-update it — it should target the most
    recent row that's still pending."""
    db = _seed(tmp_path)
    now = time.time()
    # Already-filled row
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at, affirmation_present, correction_present, "
            "reply_latency_s) VALUES (?, ?, ?, ?, 0, 0, 99.0)",
            ("filled", "sess_1", 0, now - 60),
        )
    # Pending row
    _insert_pending_row(db, "sess_1", 1, now - 8.0)

    await _backfill_prior_turn_async(
        db, session_id="sess_1", user_text="thanks", now_ts=now,
    )

    with db._connect() as conn:
        filled = conn.execute(
            "SELECT reply_latency_s FROM turn_outcomes WHERE id = 'filled'"
        ).fetchone()
        pending = conn.execute(
            "SELECT reply_latency_s, affirmation_present FROM turn_outcomes "
            "WHERE id = 'to_1'"
        ).fetchone()
    assert filled[0] == 99.0  # untouched
    assert pending[0] is not None
    assert pending[1] == 1


@pytest.mark.asyncio
async def test_backfill_swallows_db_errors(caplog):
    from unittest.mock import MagicMock

    db = MagicMock()
    db._connect.side_effect = RuntimeError("disk full")

    import logging

    caplog.set_level(logging.WARNING, logger="opencomputer.gateway.dispatch")
    await _backfill_prior_turn_async(
        db, session_id="sess_1", user_text="thanks", now_ts=time.time(),
    )
    assert any(
        "backfill failed" in r.message.lower()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_backfill_handles_both_signals(tmp_path):
    """A message that hits both affirmation AND correction patterns
    must register both — they're independent."""
    db = _seed(tmp_path)
    now = time.time()
    _insert_pending_row(db, "sess_1", 0, now - 3.0)

    await _backfill_prior_turn_async(
        db, session_id="sess_1",
        user_text="thanks but actually that's wrong",
        now_ts=now,
    )

    with db._connect() as conn:
        row = conn.execute(
            "SELECT affirmation_present, correction_present FROM turn_outcomes "
            "WHERE id = 'to_0'"
        ).fetchone()
    assert row[0] == 1
    assert row[1] == 1
