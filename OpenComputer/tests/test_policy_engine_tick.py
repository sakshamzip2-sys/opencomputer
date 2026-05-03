"""P2-7: cron engine tick — kill switch + budget + draft."""
from __future__ import annotations

import time

from opencomputer.agent.feature_flags import FeatureFlags
from opencomputer.agent.state import SessionDB
from opencomputer.cron.policy_engine_tick import (
    EngineTickResult,
    run_engine_tick,
)


def _hmac():
    return b"k" * 32


def _seed_engine_input(db, name="low", n_cites=8, mean_score=0.30):
    """Seed enough corpus + low-scoring memory that the engine returns a real rec."""
    sid = f"sess_for_{name}"
    db.create_session(sid, platform="cli", model="m")
    ep_id = db.record_episodic(session_id=sid, turn_index=0, summary=f"sum_{name}")

    with db._connect() as conn:
        # Citations of this memory at LOW score
        for i in range(n_cites):
            tsid = f"s_{name}_{i}"
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, started_at, platform, model) "
                "VALUES (?, ?, 'cli', 'm')",
                (tsid, time.time() - 86400),
            )
            to_id = f"to_{name}_{i}"
            conn.execute(
                "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                "created_at, turn_score) VALUES (?, ?, ?, ?, ?)",
                (to_id, tsid, i, time.time() - 86400, mean_score),
            )
            conn.execute(
                "INSERT INTO recall_citations (id, session_id, turn_index, "
                "episodic_event_id, candidate_kind, candidate_text_id, "
                "bm25_score, adjusted_score, retrieved_at) VALUES "
                "(?, ?, ?, ?, 'episodic', NULL, -1.0, -1.0, ?)",
                (f"rc_{name}_{i}", tsid, i, ep_id, time.time() - 86400),
            )

        # Need ≥3 candidates with scores for corpus_median to compute
        for j in range(2):
            other_sid = f"sess_other_{j}"
            db.create_session(other_sid, platform="cli", model="m")
            other_ep = db.record_episodic(
                session_id=other_sid, turn_index=0, summary=f"other_{j}",
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
                    (f"to_other_{j}_{i}", tsid, i, time.time() - 86400),
                )
                conn.execute(
                    "INSERT INTO recall_citations (id, session_id, turn_index, "
                    "episodic_event_id, candidate_kind, candidate_text_id, "
                    "bm25_score, adjusted_score, retrieved_at) VALUES "
                    "(?, ?, ?, ?, 'episodic', NULL, -1.0, -1.0, ?)",
                    (f"rc_other_{j}_{i}", tsid, i, other_ep, time.time() - 86400),
                )
    return ep_id


def test_kill_switch_disabled_returns_kill_switch_off(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.enabled", False)
    assert run_engine_tick(db=db, flags=flags, hmac_key=_hmac()) == EngineTickResult.KILL_SWITCH_OFF


def test_daily_budget_exhausted(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.daily_change_budget", 1)

    # Insert 1 active change from today
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status) VALUES ('x', ?, ?, 'recall_penalty', '1', "
            "'{}', '{}', 'r', 'e', '{}', 'MostCitedBelowMedian/1', "
            "'auto_ttl', '0', 'h', 'active')",
            (time.time(), time.time()),
        )

    result = run_engine_tick(db=db, flags=flags, hmac_key=_hmac())
    assert result == EngineTickResult.BUDGET_EXHAUSTED


def test_engine_noop_passes_through(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    # Empty corpus → engine returns INSUFFICIENT_DATA → tick returns ENGINE_NOOP
    result = run_engine_tick(db=db, flags=flags, hmac_key=_hmac())
    assert result == EngineTickResult.ENGINE_NOOP


def test_phase_a_writes_pending_approval(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 100)
    _seed_engine_input(db)

    result = run_engine_tick(db=db, flags=flags, hmac_key=_hmac())
    assert result == EngineTickResult.DRAFTED_PENDING

    with db._connect() as conn:
        row = conn.execute(
            "SELECT status, approval_mode FROM policy_changes "
            "WHERE knob_kind = 'recall_penalty' "
            "ORDER BY ts_drafted DESC LIMIT 1"
        ).fetchone()
    assert row[0] == "pending_approval"
    assert row[1] == "explicit"


def test_phase_b_auto_applies_change(tmp_path):
    """When trust threshold is met, engine auto-applies + sets pending_evaluation."""
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 0)  # immediate phase B
    ep_id = _seed_engine_input(db)

    result = run_engine_tick(db=db, flags=flags, hmac_key=_hmac())
    assert result == EngineTickResult.DRAFTED_AUTO_APPLIED

    with db._connect() as conn:
        row = conn.execute(
            "SELECT status, approval_mode, ts_applied FROM policy_changes "
            "ORDER BY ts_drafted DESC LIMIT 1"
        ).fetchone()
        ep_penalty = conn.execute(
            "SELECT recall_penalty FROM episodic_events WHERE id = ?",
            (ep_id,),
        ).fetchone()[0]
    assert row[0] == "pending_evaluation"
    assert row[1] == "auto_ttl"
    assert row[2] is not None  # ts_applied set
    assert ep_penalty == 0.20  # +0.20 from default 0
