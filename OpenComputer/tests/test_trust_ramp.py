"""P2-6: progressive trust ramp transitions phase A → phase B."""
from __future__ import annotations

import time

from opencomputer.agent.feature_flags import FeatureFlags
from opencomputer.agent.state import SessionDB
from opencomputer.agent.trust_ramp import TrustRamp


def _seed_policy_change(db, status, applied_ago_s=86400, change_id=None):
    cid = change_id or f"c_{status}_{applied_ago_s}"
    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status) VALUES (?, ?, ?, 'recall_penalty', '1', "
            "'{}', '{}', 'r', 'e', '{}', 'MostCitedBelowMedian/1', "
            "'auto_ttl', '0', 'h', ?)",
            (cid, now - applied_ago_s, now - applied_ago_s, status),
        )


def test_phase_a_until_n_safe_decisions(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    ramp = TrustRamp(db, flags)
    assert ramp.is_phase_a()
    assert ramp.next_approval_mode() == "explicit"


def test_phase_b_after_n_decayed_decisions(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 3)

    for i in range(3):
        _seed_policy_change(db, "expired_decayed", change_id=f"c{i}")

    ramp = TrustRamp(db, flags)
    assert ramp.is_phase_b()
    assert ramp.next_approval_mode() == "auto_ttl"


def test_phase_b_after_n_long_active_decisions(tmp_path):
    """A change in 'active' for ≥30 days also counts as safe."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 1)

    _seed_policy_change(
        db, "active", applied_ago_s=31 * 86400, change_id="long_active",
    )

    ramp = TrustRamp(db, flags)
    assert ramp.safe_decision_count() == 1
    assert ramp.is_phase_b()


def test_recently_active_does_not_count(tmp_path):
    """Changes in 'active' for <30 days are NOT yet safe decisions."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 1)

    _seed_policy_change(
        db, "active", applied_ago_s=10 * 86400, change_id="recent_active",
    )

    ramp = TrustRamp(db, flags)
    assert ramp.safe_decision_count() == 0
    assert ramp.is_phase_a()


def test_reverted_does_not_count_as_safe(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 1)

    _seed_policy_change(db, "reverted", change_id="reverted_id")

    ramp = TrustRamp(db, flags)
    assert ramp.is_phase_a()  # reverted doesn't count


def test_pending_approval_does_not_count(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 1)

    _seed_policy_change(db, "pending_approval", change_id="pending_id")

    ramp = TrustRamp(db, flags)
    assert ramp.is_phase_a()
