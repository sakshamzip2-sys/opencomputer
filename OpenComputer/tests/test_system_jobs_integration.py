"""Verifies the two integration gaps are closed:

1. recall_penalty actually suppresses BM25 ranking in search_episodic.
2. run_system_tick fires every system job once, idempotent on re-run.
"""
from __future__ import annotations

import time

from opencomputer.agent.state import SessionDB


def test_search_episodic_applies_recall_penalty(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s1", platform="cli", model="m")
    high = db.record_episodic(
        session_id="s1", turn_index=0, summary="kubernetes deployment",
    )
    low = db.record_episodic(
        session_id="s1", turn_index=1, summary="kubernetes pod",
    )

    # No penalty → both surfaced
    rows = db.search_episodic("kubernetes")
    assert len(rows) == 2

    # Apply heavy penalty to `low` — should drop in ranking
    with db._connect() as conn:
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = 0.9, "
            "recall_penalty_updated_at = ? WHERE id = ?",
            (time.time(), low),
        )

    rows_after = db.search_episodic("kubernetes")
    # `high` should now have a higher adjusted_score than `low`
    high_score = next(r["adjusted_score"] for r in rows_after if r["id"] == high)
    low_score = next(r["adjusted_score"] for r in rows_after if r["id"] == low)
    assert high_score > low_score
    # Floor at 0.05 means low isn't literally unreachable
    assert low_score > 0


def test_search_episodic_decayed_penalty_returns_near_neutral(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s1", platform="cli", model="m")
    aged = db.record_episodic(
        session_id="s1", turn_index=0, summary="rate limiting",
    )
    fresh = db.record_episodic(
        session_id="s1", turn_index=1, summary="rate limiting",
    )

    # Both have the same query match. aged got penalty 60d ago, fresh now.
    with db._connect() as conn:
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = 0.5, "
            "recall_penalty_updated_at = ? WHERE id = ?",
            (time.time() - 60 * 86400, aged),
        )
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = 0.5, "
            "recall_penalty_updated_at = ? WHERE id = ?",
            (time.time(), fresh),
        )

    rows = db.search_episodic("rate limiting")
    # aged decayed back to near-neutral; fresh is still suppressed
    aged_s = next(r["adjusted_score"] for r in rows if r["id"] == aged)
    fresh_s = next(r["adjusted_score"] for r in rows if r["id"] == fresh)
    assert aged_s > fresh_s


def test_run_system_tick_is_callable_and_idempotent(tmp_path, monkeypatch):
    """Smoke test: system_tick runs all 5 jobs without raising on an
    empty profile, and re-running it is harmless."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.cron.system_jobs import run_system_tick

    summary1 = run_system_tick()
    summary2 = run_system_tick()

    expected_keys = {
        "sweep_self_cancels",
        "sweep_abandonments",
        "auto_revert",
        "decay_sweep",
        "policy_engine_tick",
    }
    assert expected_keys.issubset(summary1.keys())
    assert expected_keys.issubset(summary2.keys())

    # On an empty profile every job should report 0 or "engine_noop"
    # (never an "error: ..." string).
    for name, val in summary1.items():
        assert not (isinstance(val, str) and val.startswith("error:")), (
            f"{name} errored: {val}"
        )
