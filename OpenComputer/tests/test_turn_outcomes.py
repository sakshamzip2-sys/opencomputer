"""Phase 0 of outcome-aware learning: turn_outcomes schema (migration v7).

Acceptance #1: every completed turn produces exactly one turn_outcomes row.
This module owns the schema-level guarantees; the integration acceptance
test for "actually written by the dispatch hook" lives in
test_dispatch_records_turn_outcome.py.
"""
from __future__ import annotations

from opencomputer.agent.state import SessionDB


def _cols(db: SessionDB, table: str) -> set[str]:
    with db._connect() as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indices(db: SessionDB, table: str) -> set[str]:
    with db._connect() as conn:
        return {
            r[1]
            for r in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name=?",
                (table,),
            ).fetchall()
        }


def test_schema_v7_creates_turn_outcomes_table(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    cols = _cols(db, "turn_outcomes")
    expected = {
        "id",
        "session_id",
        "turn_index",
        "created_at",
        "tool_call_count",
        "tool_success_count",
        "tool_error_count",
        "tool_blocked_count",
        "self_cancel_count",
        "retry_count",
        "vibe_before",
        "vibe_after",
        "reply_latency_s",
        "affirmation_present",
        "correction_present",
        "conversation_abandoned",
        "standing_order_violations",
        "duration_s",
        "schema_version",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_schema_v7_indices_present(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    indices = _indices(db, "turn_outcomes")
    assert "idx_turn_outcomes_session" in indices
    assert "idx_turn_outcomes_created" in indices


def test_schema_v7_recall_citations_table(tmp_path):
    """BLOCKER #2 fix: recall_citations linkage table for engine eligibility."""
    db = SessionDB(tmp_path / "s.db")
    cols = _cols(db, "recall_citations")
    expected = {
        "id",
        "session_id",
        "turn_index",
        "episodic_event_id",
        "candidate_kind",
        "candidate_text_id",
        "bm25_score",
        "adjusted_score",
        "retrieved_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_schema_version_at_or_above_7(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    with db._connect() as conn:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    assert row is not None
    assert row[0] >= 7


def test_schema_v8_adds_scoring_columns(tmp_path):
    """Phase 1 of outcome-aware learning: scoring columns."""
    db = SessionDB(tmp_path / "s.db")
    cols = _cols(db, "turn_outcomes")
    expected = {"composite_score", "judge_score", "judge_reasoning",
                "judge_model", "turn_score", "scored_at"}
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_turn_outcomes_fk_cascade_on_session_delete(tmp_path):
    """When a session is deleted, its turn_outcomes rows must cascade."""
    import time

    db = SessionDB(tmp_path / "s.db")
    sid = "sess_cascade_1"
    db.create_session(session_id=sid, platform="cli", model="opus", cwd=str(tmp_path))
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, created_at) "
            "VALUES ('a', ?, 0, ?)",
            (sid, time.time()),
        )
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT 1 FROM turn_outcomes WHERE session_id = ?", (sid,)
        ).fetchall()
    assert rows == []
