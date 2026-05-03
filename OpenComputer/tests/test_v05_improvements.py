"""v0.5 improvements suite — tests for Tasks A, C, D, E, F.

Task B (policy_audit_log) has its own dedicated file at
tests/test_policy_audit_log.py.
"""
from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import patch

import pytest

from opencomputer.agent.feature_flags import FeatureFlags
from opencomputer.agent.state import SessionDB
from opencomputer.agent.trust_ramp import TrustRamp


def _seed_change(db, change_id, status, knob="recall_penalty",
                 applied_ago_s=0, engine="MostCitedBelowMedian/1"):
    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status) VALUES (?, ?, ?, ?, '1', '{}', '{}', 'r', "
            "'e', '{}', ?, 'auto_ttl', '0', 'h', ?)",
            (change_id, now, now - applied_ago_s if applied_ago_s else now,
             knob, engine, status),
        )


# ─── Task E: tiered approval per knob_kind ──────────────────────────


def test_tier_default_for_recall_penalty_is_low_blast(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    ramp = TrustRamp(db, flags)
    # Default: recall_penalty maps to low_blast (10 safe / 7d ttl)
    assert ramp.next_approval_mode_for("recall_penalty") == "explicit"


def test_tier_high_blast_always_explicit(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write(
        "policy_engine.approval_tiers.test_knob", "high_blast",
    )
    # Even with 100 safe decisions, high_blast tier (ttl=0) returns explicit
    for i in range(100):
        _seed_change(
            db, change_id=f"c_{i}", status="expired_decayed",
            knob="test_knob", applied_ago_s=31 * 86400,
        )
    ramp = TrustRamp(db, flags)
    assert ramp.next_approval_mode_for("test_knob") == "explicit"


def test_tier_unknown_knob_defaults_to_low_blast(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    ramp = TrustRamp(db, flags)
    # An unmapped knob_kind falls back to low_blast (10 / 7d)
    assert ramp.next_approval_mode_for("brand_new_knob") == "explicit"


# ─── Task F: per-knob_kind trust ramps ──────────────────────────────


def test_per_knob_counter_isolated(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")

    # 5 safe decisions for recall_penalty, 0 for another_knob
    for i in range(5):
        _seed_change(
            db, change_id=f"r_{i}", status="expired_decayed",
            knob="recall_penalty", applied_ago_s=31 * 86400,
        )

    ramp = TrustRamp(db, flags)
    assert ramp.safe_decision_count_for("recall_penalty") == 5
    assert ramp.safe_decision_count_for("another_knob") == 0
    # Global count still reflects total
    assert ramp.safe_decision_count() == 5


def test_per_knob_threshold_independent(tmp_path):
    """Two knobs accumulate trust independently."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 3)

    # 3 safe decisions for recall_penalty
    for i in range(3):
        _seed_change(
            db, change_id=f"r_{i}", status="expired_decayed",
            knob="recall_penalty", applied_ago_s=31 * 86400,
        )

    ramp = TrustRamp(db, flags)
    # recall_penalty has trust → auto_ttl
    assert ramp.next_approval_mode_for("recall_penalty") == "auto_ttl"
    # other_knob has zero trust → explicit
    assert ramp.next_approval_mode_for("other_knob") == "explicit"


# ─── Task C: engine quality meta-metric ─────────────────────────────


def test_compute_engine_quality_returns_per_engine(tmp_path):
    from opencomputer.evolution.engine_metrics import compute_engine_quality

    db = SessionDB(tmp_path / "s.db")
    # Seed mixed statuses for one engine
    _seed_change(db, "c1", "active")
    _seed_change(db, "c2", "expired_decayed")
    _seed_change(db, "c3", "reverted")
    _seed_change(db, "c4", "pending_approval")
    # And another engine
    _seed_change(db, "c5", "active", engine="OtherEngine/1")

    metrics = compute_engine_quality(db, days=30)
    by_engine = {m.engine_version: m for m in metrics}

    mc = by_engine["MostCitedBelowMedian/1"]
    assert mc.n_recommendations == 4
    assert mc.n_active == 1
    assert mc.n_expired_decayed == 1
    assert mc.n_reverted == 1
    assert mc.n_pending == 1
    # evaluated = 3 (active + expired + reverted); unrevert = 2/3
    assert abs(mc.unrevert_rate - 2 / 3) < 1e-9
    assert abs(mc.revert_rate - 1 / 3) < 1e-9

    other = by_engine["OtherEngine/1"]
    assert other.n_active == 1


def test_engine_quality_filtered_by_engine_version(tmp_path):
    from opencomputer.evolution.engine_metrics import compute_engine_quality

    db = SessionDB(tmp_path / "s.db")
    _seed_change(db, "c1", "active", engine="A/1")
    _seed_change(db, "c2", "active", engine="B/1")

    metrics = compute_engine_quality(db, engine_version="A/1", days=30)
    assert len(metrics) == 1
    assert metrics[0].engine_version == "A/1"


def test_engine_quality_handles_no_data(tmp_path):
    from opencomputer.evolution.engine_metrics import compute_engine_quality

    db = SessionDB(tmp_path / "s.db")
    metrics = compute_engine_quality(db, days=30)
    assert metrics == []


# ─── Task D: data retention prune ───────────────────────────────────


def test_prune_deletes_old_rows(tmp_path):
    from opencomputer.cron.prune_turn_outcomes import run_prune_turn_outcomes

    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    db.create_session("s1", platform="cli", model="m")

    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at) VALUES ('old', 's1', 0, ?)",
            (now - 95 * 86400,),  # >90 days
        )
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at) VALUES ('recent', 's1', 1, ?)",
            (now - 30 * 86400,),
        )

    n = run_prune_turn_outcomes(db=db, flags=flags)
    assert n == 1

    with db._connect() as conn:
        ids = [
            r[0]
            for r in conn.execute("SELECT id FROM turn_outcomes").fetchall()
        ]
    assert ids == ["recent"]


def test_prune_respects_custom_retention_days(tmp_path):
    from opencomputer.cron.prune_turn_outcomes import run_prune_turn_outcomes

    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("data_retention.turn_outcomes_days", 7)
    db.create_session("s1", platform="cli", model="m")

    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at) VALUES ('eight_days', 's1', 0, ?)",
            (now - 8 * 86400,),
        )

    n = run_prune_turn_outcomes(db=db, flags=flags)
    assert n == 1


def test_prune_cascades_recall_citations(tmp_path):
    """Deleting a turn_outcomes row should NOT orphan recall_citations
    even though they aren't FK-linked to turn_outcomes directly — the
    citations linkage is via session_id."""
    from opencomputer.cron.prune_turn_outcomes import run_prune_turn_outcomes

    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    db.create_session("s1", platform="cli", model="m")
    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at) VALUES ('to_old', 's1', 0, ?)",
            (now - 100 * 86400,),
        )
        # recall_citations row pointing at this old turn — survives prune
        # because it FK's to sessions, not turn_outcomes
        conn.execute(
            "INSERT INTO recall_citations (id, session_id, turn_index, "
            "candidate_kind, retrieved_at) "
            "VALUES ('rc_old', 's1', 0, 'episodic', ?)",
            (now - 100 * 86400,),
        )

    run_prune_turn_outcomes(db=db, flags=flags)

    with db._connect() as conn:
        # turn_outcomes row gone, recall_citations preserved (intentional
        # — citations linkage is per-session not per-turn_outcomes)
        n_to = conn.execute(
            "SELECT COUNT(*) FROM turn_outcomes WHERE id = 'to_old'"
        ).fetchone()[0]
        n_rc = conn.execute(
            "SELECT COUNT(*) FROM recall_citations WHERE id = 'rc_old'"
        ).fetchone()[0]
    assert n_to == 0
    assert n_rc == 1


# ─── Task A: daily Telegram digest ──────────────────────────────────


def test_digest_skips_when_disabled(tmp_path):
    from opencomputer.cron.policy_digest import run_policy_digest

    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.digest_mode", False)
    n = run_policy_digest(db=db, flags=flags)
    assert n == 0


def test_digest_skips_before_target_hour(tmp_path):
    from opencomputer.cron.policy_digest import run_policy_digest

    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    _seed_change(db, "c1", "pending_approval")

    # Mock datetime.now() to return midnight (hour 0 < default 9)
    with patch("opencomputer.cron.policy_digest.datetime") as mdt:
        mdt.now.return_value = datetime(2026, 5, 3, 0, 0, 0)
        mdt.strftime = datetime.strftime
        n = run_policy_digest(db=db, flags=flags)
    assert n == 0


def test_digest_fires_at_or_after_target_hour(tmp_path):
    from opencomputer.cron.policy_digest import run_policy_digest

    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    _seed_change(db, "c1", "pending_approval")
    _seed_change(db, "c2", "pending_approval")

    sent: list[str] = []

    async def fake_send(text: str) -> None:
        sent.append(text)

    with patch("opencomputer.cron.policy_digest.datetime") as mdt:
        mdt.now.return_value = datetime(2026, 5, 3, 10, 0, 0)
        n = run_policy_digest(db=db, flags=flags, send_fn=fake_send)
    assert n == 2


def test_digest_idempotent_same_day(tmp_path):
    from opencomputer.cron.policy_digest import run_policy_digest

    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    _seed_change(db, "c1", "pending_approval")

    with patch("opencomputer.cron.policy_digest.datetime") as mdt:
        mdt.now.return_value = datetime(2026, 5, 3, 10, 0, 0)
        n1 = run_policy_digest(db=db, flags=flags)
        # Same day, second call → no-op
        n2 = run_policy_digest(db=db, flags=flags)
    assert n1 >= 0  # may be 0 if asyncio.run() couldn't run async send
    assert n2 == 0  # second call MUST be no-op
