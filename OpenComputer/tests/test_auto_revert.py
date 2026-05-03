"""P2-8: statistical auto-revert with N=10 + 1σ gate."""
from __future__ import annotations

import time

from opencomputer.agent.feature_flags import FeatureFlags
from opencomputer.agent.state import SessionDB
from opencomputer.cron.auto_revert import run_auto_revert_due


def _hmac():
    return b"k" * 32


def _seed_active_change(
    db, change_id, applied_at, baseline_mean, baseline_std, target_ep,
):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status, pre_change_baseline_mean, "
            "pre_change_baseline_std) VALUES (?, ?, ?, 'recall_penalty', ?, "
            "'{\"recall_penalty\":0.0}', '{\"recall_penalty\":0.2}', 'r', "
            "'e', '{\"action\":\"set\",\"field\":\"recall_penalty\","
            "\"value\":0.0}', 'MostCitedBelowMedian/1', 'auto_ttl', '0', "
            "'h', 'pending_evaluation', ?, ?)",
            (change_id, applied_at, applied_at, target_ep,
             baseline_mean, baseline_std),
        )


def _seed_episodic_with_penalty(db, penalty):
    db.create_session("ses_target", platform="cli", model="m")
    ep_id = db.record_episodic(
        session_id="ses_target", turn_index=0, summary="target",
    )
    with db._connect() as conn:
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = ?, "
            "recall_penalty_updated_at = ? WHERE id = ?",
            (penalty, time.time(), ep_id),
        )
    return ep_id


def _seed_post_change_turns(db, n: int, mean_score: float, applied_at: float):
    db.create_session("ses_post", platform="cli", model="m")
    with db._connect() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                "created_at, turn_score) VALUES (?, ?, ?, ?, ?)",
                (f"to_post_{i}", "ses_post", i,
                 applied_at + 600 * i, mean_score),
            )


def test_under_n_threshold_keeps_pending(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    applied = time.time() - 86400
    ep = _seed_episodic_with_penalty(db, penalty=0.2)
    _seed_active_change(db, "c1", applied, 0.6, 0.1, target_ep=ep)
    _seed_post_change_turns(db, n=5, mean_score=0.2, applied_at=applied)

    run_auto_revert_due(db=db, flags=flags, hmac_key=_hmac())

    with db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM policy_changes WHERE id = 'c1'"
        ).fetchone()[0]
    assert status == "pending_evaluation"  # HARD GATE: N < 10


def test_post_below_baseline_minus_1sigma_reverts(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    applied = time.time() - 86400
    ep = _seed_episodic_with_penalty(db, penalty=0.2)
    _seed_active_change(db, "c1", applied, 0.6, 0.1, target_ep=ep)
    # 12 turns at mean 0.4 = baseline 0.6 - 2σ (σ=0.1) → revert
    _seed_post_change_turns(db, n=12, mean_score=0.4, applied_at=applied)

    run_auto_revert_due(db=db, flags=flags, hmac_key=_hmac())

    with db._connect() as conn:
        row = conn.execute(
            "SELECT status, reverted_reason FROM policy_changes WHERE id = 'c1'"
        ).fetchone()
        penalty = conn.execute(
            "SELECT recall_penalty FROM episodic_events WHERE id = ?",
            (ep,),
        ).fetchone()[0]
    assert row["status"] == "reverted"
    assert "statistical" in (row["reverted_reason"] or "").lower()
    assert penalty == 0.0  # rolled back


def test_post_within_1sigma_marks_active(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    applied = time.time() - 86400
    ep = _seed_episodic_with_penalty(db, penalty=0.2)
    _seed_active_change(db, "c1", applied, 0.6, 0.1, target_ep=ep)
    _seed_post_change_turns(db, n=12, mean_score=0.55, applied_at=applied)

    run_auto_revert_due(db=db, flags=flags, hmac_key=_hmac())

    with db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM policy_changes WHERE id = 'c1'"
        ).fetchone()[0]
    assert status == "active"


def test_eligible_turn_count_updates(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    applied = time.time() - 86400
    ep = _seed_episodic_with_penalty(db, penalty=0.2)
    _seed_active_change(db, "c1", applied, 0.6, 0.1, target_ep=ep)
    _seed_post_change_turns(db, n=4, mean_score=0.5, applied_at=applied)

    run_auto_revert_due(db=db, flags=flags, hmac_key=_hmac())

    with db._connect() as conn:
        n = conn.execute(
            "SELECT eligible_turn_count FROM policy_changes WHERE id='c1'"
        ).fetchone()[0]
    assert n == 4
