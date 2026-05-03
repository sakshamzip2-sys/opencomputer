"""P2-10: /policy-changes, /policy-approve, /policy-revert."""
from __future__ import annotations

import time

import pytest

from opencomputer.agent.slash_commands_impl.policy import (
    handle_policy_approve,
    handle_policy_changes,
    handle_policy_revert,
)
from opencomputer.agent.state import SessionDB


def _hmac():
    return b"k" * 32


def _seed_target_episodic(db):
    db.create_session("ses_target", platform="cli", model="m")
    return db.record_episodic(
        session_id="ses_target", turn_index=0, summary="target",
    )


def _seed_change(db, status, change_id, target_ep, new_pen=0.2):
    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status) VALUES (?, ?, ?, 'recall_penalty', ?, "
            "'{\"recall_penalty\":0.0}', ?, 'engine reason', 'e', "
            "'{\"action\":\"set\",\"field\":\"recall_penalty\","
            "\"value\":0.0}', 'MostCitedBelowMedian/1', ?, '0', 'h', ?)",
            (change_id, now, now if status != "pending_approval" else None,
             target_ep,
             f'{{"recall_penalty":{new_pen}}}',
             "explicit" if status == "pending_approval" else "auto_ttl",
             status),
        )


@pytest.mark.asyncio
async def test_policy_changes_lists_recent(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    ep = _seed_target_episodic(db)
    _seed_change(db, "active", "c1", ep)

    out = await handle_policy_changes(db=db, args="--days 7")
    assert "MostCitedBelowMedian/1" in out.text
    assert "engine reason" in out.text
    assert "active" in out.text


@pytest.mark.asyncio
async def test_policy_changes_empty(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    out = await handle_policy_changes(db=db)
    assert "no policy changes" in out.text.lower()


@pytest.mark.asyncio
async def test_policy_approve_transitions_to_pending_evaluation(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    ep = _seed_target_episodic(db)
    _seed_change(db, "pending_approval", "p1", ep)

    out = await handle_policy_approve(db=db, args="p1", hmac_key=_hmac())
    assert "approved" in out.text.lower()

    with db._connect() as conn:
        row = conn.execute(
            "SELECT pc.status, ee.recall_penalty FROM policy_changes pc "
            "JOIN episodic_events ee ON ee.id = pc.target_id "
            "WHERE pc.id = 'p1'"
        ).fetchone()
    assert row[0] == "pending_evaluation"
    assert abs(row[1] - 0.2) < 1e-9


@pytest.mark.asyncio
async def test_policy_approve_rejects_non_pending(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    ep = _seed_target_episodic(db)
    _seed_change(db, "active", "a1", ep)

    out = await handle_policy_approve(db=db, args="a1", hmac_key=_hmac())
    assert out.ok is False


@pytest.mark.asyncio
async def test_policy_revert_works_at_active_state(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    ep = _seed_target_episodic(db)
    _seed_change(db, "active", "a1", ep)
    with db._connect() as conn:
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = 0.2 WHERE id = ?",
            (ep,),
        )

    out = await handle_policy_revert(db=db, args="a1", hmac_key=_hmac())
    assert "reverted" in out.text.lower()

    with db._connect() as conn:
        row = conn.execute(
            "SELECT pc.status, ee.recall_penalty FROM policy_changes pc "
            "JOIN episodic_events ee ON ee.id = pc.target_id "
            "WHERE pc.id = 'a1'"
        ).fetchone()
    assert row[0] == "reverted"
    assert row[1] == 0.0


@pytest.mark.asyncio
async def test_policy_revert_rejects_already_reverted(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    ep = _seed_target_episodic(db)
    _seed_change(db, "reverted", "r1", ep)

    out = await handle_policy_revert(db=db, args="r1", hmac_key=_hmac())
    assert out.ok is False
