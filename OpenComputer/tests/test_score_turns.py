"""Phase 1 scoring cron — composite + (optional) judge + turn_score backfill."""
from __future__ import annotations

import time

from opencomputer.agent.state import SessionDB
from opencomputer.cron import score_turns


def _seed_unscored_row(db, row_id="r1", **overrides):
    sid = overrides.get("session_id", "ses1")
    db.create_session(sid, platform="cli", model="m")
    cols = {
        "id": row_id,
        "session_id": sid,
        "turn_index": 0,
        "created_at": time.time(),
        "tool_call_count": 1,
        "tool_success_count": 1,
        "tool_error_count": 0,
        "tool_blocked_count": 0,
        "self_cancel_count": 0,
        "retry_count": 0,
        "vibe_before": None,
        "vibe_after": None,
        "reply_latency_s": None,
        "affirmation_present": 0,
        "correction_present": 0,
        "conversation_abandoned": 0,
        "standing_order_violations": None,
        "duration_s": 1.0,
    }
    cols.update(overrides)
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes ("
            + ", ".join(cols.keys())
            + ") VALUES ("
            + ", ".join("?" * len(cols))
            + ")",
            tuple(cols.values()),
        )


def test_score_turns_writes_composite_for_silent_turn(tmp_path, monkeypatch):
    """Disable the LLM judge so we can verify composite-only path
    deterministically."""
    monkeypatch.setattr(score_turns, "_JUDGE_ENABLED", False)

    db = SessionDB(tmp_path / "s.db")
    _seed_unscored_row(db, row_id="r1")

    summary = score_turns.run_score_turns(db=db)
    assert summary["composite_only"] == 1

    with db._connect() as conn:
        row = conn.execute(
            "SELECT composite_score, judge_score, turn_score, scored_at "
            "FROM turn_outcomes WHERE id = 'r1'"
        ).fetchone()
    assert row[0] is not None
    assert 0.0 <= row[0] <= 1.0
    assert row[1] is None  # judge disabled
    assert abs(row[2] - row[0]) < 1e-9  # turn_score == composite when judge None
    assert row[3] is not None


def test_score_turns_only_picks_unscored(tmp_path, monkeypatch):
    monkeypatch.setattr(score_turns, "_JUDGE_ENABLED", False)
    db = SessionDB(tmp_path / "s.db")

    _seed_unscored_row(db, row_id="r1")
    _seed_unscored_row(db, row_id="r2")
    # Pre-score r2
    with db._connect() as conn:
        conn.execute(
            "UPDATE turn_outcomes SET turn_score = 0.99, "
            "composite_score = 0.99, scored_at = ? WHERE id = 'r2'",
            (time.time(),),
        )

    score_turns.run_score_turns(db=db)

    with db._connect() as conn:
        r1_score = conn.execute(
            "SELECT turn_score FROM turn_outcomes WHERE id = 'r1'"
        ).fetchone()[0]
        r2_score = conn.execute(
            "SELECT turn_score FROM turn_outcomes WHERE id = 'r2'"
        ).fetchone()[0]
    assert r1_score is not None
    assert r2_score == 0.99  # untouched


def test_score_turns_idempotent(tmp_path, monkeypatch):
    """Re-running the cron must not re-score already-scored rows."""
    monkeypatch.setattr(score_turns, "_JUDGE_ENABLED", False)
    db = SessionDB(tmp_path / "s.db")
    _seed_unscored_row(db, row_id="r1")

    score_turns.run_score_turns(db=db)
    with db._connect() as conn:
        first_scored_at = conn.execute(
            "SELECT scored_at FROM turn_outcomes WHERE id = 'r1'"
        ).fetchone()[0]

    score_turns.run_score_turns(db=db)
    with db._connect() as conn:
        second_scored_at = conn.execute(
            "SELECT scored_at FROM turn_outcomes WHERE id = 'r1'"
        ).fetchone()[0]
    assert first_scored_at == second_scored_at  # untouched on re-run


def test_score_turns_correction_lowers_score(tmp_path, monkeypatch):
    monkeypatch.setattr(score_turns, "_JUDGE_ENABLED", False)
    db = SessionDB(tmp_path / "s.db")
    _seed_unscored_row(
        db, row_id="ok",
        affirmation_present=1, correction_present=0,
    )
    _seed_unscored_row(
        db, row_id="bad",
        session_id="ses2",
        affirmation_present=0, correction_present=1,
        tool_error_count=2, tool_success_count=0,
    )

    score_turns.run_score_turns(db=db)

    with db._connect() as conn:
        ok_s = conn.execute(
            "SELECT turn_score FROM turn_outcomes WHERE id = 'ok'"
        ).fetchone()[0]
        bad_s = conn.execute(
            "SELECT turn_score FROM turn_outcomes WHERE id = 'bad'"
        ).fetchone()[0]
    assert ok_s > bad_s


def test_score_turns_skips_old_rows(tmp_path, monkeypatch):
    """Rows older than _LOOKBACK_S are not backfilled."""
    monkeypatch.setattr(score_turns, "_JUDGE_ENABLED", False)
    db = SessionDB(tmp_path / "s.db")
    old_ts = time.time() - 25 * 3600  # >24h ago
    _seed_unscored_row(db, row_id="old", created_at=old_ts)

    summary = score_turns.run_score_turns(db=db)
    assert summary["composite_only"] == 0

    with db._connect() as conn:
        row = conn.execute(
            "SELECT turn_score FROM turn_outcomes WHERE id = 'old'"
        ).fetchone()
    assert row[0] is None
