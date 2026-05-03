"""P2-9: decay sweep — active→expired_decayed + pending discard."""
from __future__ import annotations

import time

from opencomputer.agent.state import SessionDB
from opencomputer.cron.decay_sweep import run_decay_sweep


def test_active_with_decayed_penalty_marks_expired(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    now = time.time()
    db.create_session("s1", platform="cli", model="m")
    ep_id = db.record_episodic(session_id="s1", turn_index=0, summary="x")
    with db._connect() as conn:
        # 90 days old, penalty 0.005 (well below 0.05 floor after decay)
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = 0.005, "
            "recall_penalty_updated_at = ? WHERE id = ?",
            (now - 90 * 86400, ep_id),
        )
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status) VALUES ('c1', ?, ?, 'recall_penalty', ?, "
            "'{}', '{\"recall_penalty\":0.2}', 'r', 'e', '{}', "
            "'MostCitedBelowMedian/1', 'auto_ttl', '0', 'h', 'active')",
            (now - 90 * 86400, now - 90 * 86400, ep_id),
        )

    result = run_decay_sweep(db=db, hmac_key=b"k" * 32)
    assert result.expired_count == 1

    with db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM policy_changes WHERE id = 'c1'"
        ).fetchone()[0]
    assert status == "expired_decayed"


def test_recent_active_change_stays_active(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    now = time.time()
    db.create_session("s1", platform="cli", model="m")
    ep_id = db.record_episodic(session_id="s1", turn_index=0, summary="x")
    with db._connect() as conn:
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = 0.2, "
            "recall_penalty_updated_at = ? WHERE id = ?",
            (now, ep_id),
        )
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status) VALUES ('c1', ?, ?, 'recall_penalty', ?, "
            "'{}', '{\"recall_penalty\":0.2}', 'r', 'e', '{}', "
            "'MostCitedBelowMedian/1', 'auto_ttl', '0', 'h', 'active')",
            (now, now, ep_id),
        )

    result = run_decay_sweep(db=db, hmac_key=b"k" * 32)
    assert result.expired_count == 0

    with db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM policy_changes WHERE id = 'c1'"
        ).fetchone()[0]
    assert status == "active"


def test_pending_approval_older_than_7d_discarded(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status) VALUES ('p1', ?, NULL, 'recall_penalty', '1', "
            "'{}', '{}', 'r', 'e', '{}', 'MostCitedBelowMedian/1', "
            "'explicit', '0', 'h', 'pending_approval')",
            (now - 8 * 86400,),
        )

    result = run_decay_sweep(db=db, hmac_key=b"k" * 32)
    assert result.pending_discarded == 1

    with db._connect() as conn:
        row = conn.execute(
            "SELECT status, reverted_reason FROM policy_changes WHERE id = 'p1'"
        ).fetchone()
    assert row[0] == "expired_decayed"
    assert "pending_approval" in (row[1] or "").lower()


def test_recent_pending_kept(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status) VALUES ('p1', ?, NULL, 'recall_penalty', '1', "
            "'{}', '{}', 'r', 'e', '{}', 'MostCitedBelowMedian/1', "
            "'explicit', '0', 'h', 'pending_approval')",
            (now - 86400,),
        )

    result = run_decay_sweep(db=db, hmac_key=b"k" * 32)
    assert result.pending_discarded == 0
