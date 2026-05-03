"""P2 v0 acceptance criteria — end-to-end verification suite.

Each test maps directly to a numbered acceptance criterion in the spec
at OpenComputer/docs/superpowers/specs/2026-05-03-outcome-aware-learning-design.md
"""
from __future__ import annotations

import time

import pytest

from opencomputer.agent.feature_flags import FeatureFlags
from opencomputer.agent.policy_audit import PolicyAuditLogger
from opencomputer.agent.recall_synthesizer import (
    apply_recall_penalty,
    decay_factor,
)
from opencomputer.agent.state import SessionDB
from opencomputer.agent.trust_ramp import TrustRamp
from opencomputer.cron.auto_revert import run_auto_revert_due
from opencomputer.cron.decay_sweep import run_decay_sweep
from opencomputer.cron.policy_engine_tick import (
    EngineTickResult,
    run_engine_tick,
)


def _hmac():
    return b"k" * 32


def _seed_underperforming_memory(db, name="low", n_cites=8):
    sid = f"sess_for_{name}"
    db.create_session(sid, platform="cli", model="m")
    ep_id = db.record_episodic(session_id=sid, turn_index=0, summary="x")
    with db._connect() as conn:
        for i in range(n_cites):
            tsid = f"s_{name}_{i}"
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, started_at, platform, model) "
                "VALUES (?, ?, 'cli', 'm')",
                (tsid, time.time() - 86400),
            )
            conn.execute(
                "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                "created_at, turn_score) VALUES (?, ?, ?, ?, 0.30)",
                (f"to_{name}_{i}", tsid, i, time.time() - 86400),
            )
            conn.execute(
                "INSERT INTO recall_citations (id, session_id, turn_index, "
                "episodic_event_id, candidate_kind, candidate_text_id, "
                "bm25_score, adjusted_score, retrieved_at) VALUES "
                "(?, ?, ?, ?, 'episodic', NULL, -1.0, -1.0, ?)",
                (f"rc_{name}_{i}", tsid, i, ep_id, time.time() - 86400),
            )

        # Healthy peers so corpus_median computable + above threshold
        for j in range(2):
            other_sid = f"sess_o_{j}"
            db.create_session(other_sid, platform="cli", model="m")
            o_ep = db.record_episodic(
                session_id=other_sid, turn_index=0, summary=f"o{j}",
            )
            for i in range(5):
                tsid = f"so_{j}_{i}"
                conn.execute(
                    "INSERT OR IGNORE INTO sessions (id, started_at, platform, "
                    "model) VALUES (?, ?, 'cli', 'm')",
                    (tsid, time.time() - 86400),
                )
                conn.execute(
                    "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                    "created_at, turn_score) VALUES (?, ?, ?, ?, 0.7)",
                    (f"to_o_{j}_{i}", tsid, i, time.time() - 86400),
                )
                conn.execute(
                    "INSERT INTO recall_citations (id, session_id, turn_index, "
                    "episodic_event_id, candidate_kind, candidate_text_id, "
                    "bm25_score, adjusted_score, retrieved_at) VALUES "
                    "(?, ?, ?, ?, 'episodic', NULL, -1.0, -1.0, ?)",
                    (f"rc_o_{j}_{i}", tsid, i, o_ep, time.time() - 86400),
                )
    return ep_id


# ─── Phase 0 acceptance ─────────────────────────────────────────────


def test_acceptance_phase0_silent_baseline_anchors_at_05():
    """A turn with no signals ≠ score zero. Phase 1 acceptance."""
    from opencomputer.agent.composite_scorer import compute_composite_score
    score = compute_composite_score(
        tool_call_count=0, tool_success_count=0, tool_error_count=0,
        self_cancel_count=0, retry_count=0, conversation_abandoned=False,
        affirmation_present=False, correction_present=False,
        vibe_delta=0, standing_order_violation_count=0,
    )
    assert 0.45 < score < 0.55


# ─── Phase 2 v0 acceptance ──────────────────────────────────────────


def test_acceptance_10_n_gte_10_post_below_minus_sigma_reverts(tmp_path):
    """A#10: N >= 10 AND post < pre - 1σ → auto-revert fires."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    db.create_session("ses_t", platform="cli", model="m")
    ep = db.record_episodic(session_id="ses_t", turn_index=0, summary="x")

    applied = time.time() - 86400
    with db._connect() as conn:
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = 0.2, "
            "recall_penalty_updated_at = ? WHERE id = ?",
            (applied, ep),
        )
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status, pre_change_baseline_mean, "
            "pre_change_baseline_std) VALUES ('c1', ?, ?, 'recall_penalty', ?, "
            "'{\"recall_penalty\":0.0}', '{\"recall_penalty\":0.2}', 'r', "
            "'e', '{\"action\":\"set\",\"field\":\"recall_penalty\","
            "\"value\":0.0}', 'MostCitedBelowMedian/1', 'auto_ttl', '0', "
            "'h', 'pending_evaluation', 0.6, 0.1)",
            (applied, applied, ep),
        )
        # 12 post turns at mean 0.4 (= baseline 0.6 - 2σ)
        db.create_session("ses_p", platform="cli", model="m")
        for i in range(12):
            conn.execute(
                "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                "created_at, turn_score) VALUES (?, ?, ?, ?, 0.4)",
                (f"tp_{i}", "ses_p", i, applied + 600 * i),
            )

    run_auto_revert_due(db=db, flags=flags, hmac_key=_hmac())

    with db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM policy_changes WHERE id = 'c1'"
        ).fetchone()[0]
    assert status == "reverted"


def test_acceptance_11_under_n_stays_pending(tmp_path):
    """A#11: N < 10 → never auto-reverts."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    db.create_session("ses_t", platform="cli", model="m")
    ep = db.record_episodic(session_id="ses_t", turn_index=0, summary="x")

    applied = time.time() - 86400
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status, pre_change_baseline_mean, "
            "pre_change_baseline_std) VALUES ('c1', ?, ?, 'recall_penalty', ?, "
            "'{\"recall_penalty\":0.0}', '{\"recall_penalty\":0.2}', 'r', "
            "'e', '{\"action\":\"set\",\"field\":\"recall_penalty\","
            "\"value\":0.0}', 'MostCitedBelowMedian/1', 'auto_ttl', '0', "
            "'h', 'pending_evaluation', 0.6, 0.1)",
            (applied, applied, ep),
        )
        # Only 5 post turns
        db.create_session("ses_p", platform="cli", model="m")
        for i in range(5):
            conn.execute(
                "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                "created_at, turn_score) VALUES (?, ?, ?, ?, 0.2)",
                (f"tp_{i}", "ses_p", i, applied + 600 * i),
            )

    run_auto_revert_due(db=db, flags=flags, hmac_key=_hmac())

    with db._connect() as conn:
        status = conn.execute(
            "SELECT status FROM policy_changes WHERE id = 'c1'"
        ).fetchone()[0]
    assert status == "pending_evaluation"


def test_acceptance_13_first_recommendations_require_explicit_approval(tmp_path):
    """A#13: With no prior safe decisions, recommendations land as
    pending_approval, not auto-applied."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 10)
    _seed_underperforming_memory(db)

    result = run_engine_tick(db=db, flags=flags, hmac_key=_hmac())
    assert result == EngineTickResult.DRAFTED_PENDING

    with db._connect() as conn:
        row = conn.execute(
            "SELECT status, approval_mode FROM policy_changes "
            "ORDER BY ts_drafted DESC LIMIT 1"
        ).fetchone()
    assert row[0] == "pending_approval"
    assert row[1] == "explicit"


def test_acceptance_15_phase_b_after_n_safe_decisions_auto_applies(tmp_path):
    """A#15: After N safe decisions, recommendations auto-apply with TTL."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 0)
    _seed_underperforming_memory(db)

    result = run_engine_tick(db=db, flags=flags, hmac_key=_hmac())
    assert result == EngineTickResult.DRAFTED_AUTO_APPLIED

    ramp = TrustRamp(db, flags)
    assert ramp.is_phase_b()


def test_acceptance_17_engine_version_recorded(tmp_path):
    """A#17: Every policy_changes row carries recommendation_engine_version."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    _seed_underperforming_memory(db)
    run_engine_tick(db=db, flags=flags, hmac_key=_hmac())

    with db._connect() as conn:
        v = conn.execute(
            "SELECT recommendation_engine_version FROM policy_changes "
            "ORDER BY ts_drafted DESC LIMIT 1"
        ).fetchone()
    assert v is not None
    assert v[0] == "MostCitedBelowMedian/1"


def test_acceptance_18_quiet_corpus_emits_zero_changes(tmp_path):
    """A#18: No candidate exceeds threshold → engine emits zero changes."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    # Empty corpus
    result = run_engine_tick(db=db, flags=flags, hmac_key=_hmac())
    assert result == EngineTickResult.ENGINE_NOOP

    with db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM policy_changes").fetchone()[0]
    assert n == 0


def test_acceptance_20_kill_switch_halts_drafts(tmp_path):
    """A#20: feature_flags.policy_engine.enabled=false halts new drafts."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.enabled", False)
    _seed_underperforming_memory(db)

    result = run_engine_tick(db=db, flags=flags, hmac_key=_hmac())
    assert result == EngineTickResult.KILL_SWITCH_OFF

    with db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM policy_changes").fetchone()[0]
    assert n == 0


def test_acceptance_22_daily_budget_caps_changes(tmp_path):
    """A#22: Daily budget caps at 3 changes per 24h regardless of recommendation count."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.daily_change_budget", 1)

    # Insert a synthetic active change today to exhaust the budget
    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status) VALUES ('budget_used', ?, ?, "
            "'recall_penalty', '1', '{}', '{}', 'r', 'e', '{}', "
            "'MostCitedBelowMedian/1', 'auto_ttl', '0', 'h', 'active')",
            (now, now),
        )

    result = run_engine_tick(db=db, flags=flags, hmac_key=_hmac())
    assert result == EngineTickResult.BUDGET_EXHAUSTED


def test_acceptance_25_hmac_chain_validates(tmp_path):
    """A#25: policy_changes.hmac_self chain validates after every write."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    _seed_underperforming_memory(db)
    run_engine_tick(db=db, flags=flags, hmac_key=_hmac())

    with db._connect() as conn:
        log = PolicyAuditLogger(conn, hmac_key=_hmac())
        assert log.verify_chain() is True


def test_acceptance_27_decay_returns_to_neutral_after_60_days():
    """A#27: After 60 days of no further negative signal, recall_penalty
    decays to ≤0.05 (effectively neutral)."""
    # 60-day-old penalty of 0.2 → effective ≈ 0.2 * 0.05 ≈ 0.01
    aged = apply_recall_penalty(1.0, recall_penalty=0.2, age_days=60)
    fresh = apply_recall_penalty(1.0, recall_penalty=0.2, age_days=0)
    assert fresh < 0.85  # noticeable suppression
    assert aged > 0.95   # near-neutral
    # Effective penalty math
    assert decay_factor(60) * 0.2 < 0.05


def test_full_loop_decay_sweep_marks_expired(tmp_path):
    """Decay sweep correctly transitions active→expired_decayed."""
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s1", platform="cli", model="m")
    ep = db.record_episodic(session_id="s1", turn_index=0, summary="x")

    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = 0.005, "
            "recall_penalty_updated_at = ? WHERE id = ?",
            (now - 90 * 86400, ep),
        )
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status) VALUES ('c1', ?, ?, 'recall_penalty', ?, "
            "'{}', '{\"recall_penalty\":0.2}', 'r', 'e', '{}', "
            "'MostCitedBelowMedian/1', 'auto_ttl', '0', 'h', 'active')",
            (now - 90 * 86400, now - 90 * 86400, ep),
        )

    result = run_decay_sweep(db=db, hmac_key=_hmac())
    assert result.expired_count == 1
