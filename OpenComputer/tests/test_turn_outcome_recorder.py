"""P0-3: TurnOutcomeRecorder writes per-turn implicit-signal blob to DB."""
from __future__ import annotations

from opencomputer.agent.state import SessionDB
from opencomputer.agent.turn_outcome_recorder import (
    TurnOutcomeRecorder,
    TurnSignals,
)


def _make_db_with_session(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    db.create_session("sess_1", platform="cli", model="opus", cwd=str(tmp_path))
    return db


def test_record_simple_turn(tmp_path):
    db = _make_db_with_session(tmp_path)
    rec = TurnOutcomeRecorder(db)
    rec.record(
        TurnSignals(
            session_id="sess_1",
            turn_index=0,
            tool_call_count=2,
            tool_success_count=2,
            tool_error_count=0,
            duration_s=1.5,
            vibe_before="curious",
            vibe_after="curious",
            reply_latency_s=12.3,
            affirmation_present=True,
            correction_present=False,
        )
    )
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT tool_call_count, affirmation_present, vibe_before, "
            "reply_latency_s FROM turn_outcomes WHERE session_id = 'sess_1'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 2
    assert rows[0][1] == 1
    assert rows[0][2] == "curious"
    assert abs(rows[0][3] - 12.3) < 1e-9


def test_recorder_handles_missing_optional_fields(tmp_path):
    db = _make_db_with_session(tmp_path)
    rec = TurnOutcomeRecorder(db)
    rec.record(TurnSignals(session_id="sess_1", turn_index=0))
    with db._connect() as conn:
        row = conn.execute(
            "SELECT reply_latency_s, vibe_before, standing_order_violations "
            "FROM turn_outcomes WHERE session_id = 'sess_1'"
        ).fetchone()
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None  # empty tuple → NULL


def test_record_persists_standing_order_violations_as_json(tmp_path):
    import json

    db = _make_db_with_session(tmp_path)
    rec = TurnOutcomeRecorder(db)
    rec.record(
        TurnSignals(
            session_id="sess_1",
            turn_index=0,
            standing_order_violations=("be concise", "confirm before delete"),
        )
    )
    with db._connect() as conn:
        row = conn.execute(
            "SELECT standing_order_violations FROM turn_outcomes "
            "WHERE session_id = 'sess_1'"
        ).fetchone()
    assert row[0] is not None
    parsed = json.loads(row[0])
    assert parsed == ["be concise", "confirm before delete"]


def test_record_returns_uuid(tmp_path):
    db = _make_db_with_session(tmp_path)
    rec = TurnOutcomeRecorder(db)
    row_id = rec.record(TurnSignals(session_id="sess_1", turn_index=0))
    assert isinstance(row_id, str)
    assert len(row_id) == 36  # uuid v4 string length


def test_two_recordings_for_same_turn_yields_two_rows(tmp_path):
    """Acceptable: rare race; downstream queries dedup by created_at DESC."""
    db = _make_db_with_session(tmp_path)
    rec = TurnOutcomeRecorder(db)
    rec.record(TurnSignals(session_id="sess_1", turn_index=0))
    rec.record(TurnSignals(session_id="sess_1", turn_index=0))
    with db._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM turn_outcomes "
            "WHERE session_id = 'sess_1' AND turn_index = 0"
        ).fetchone()[0]
    assert n == 2
