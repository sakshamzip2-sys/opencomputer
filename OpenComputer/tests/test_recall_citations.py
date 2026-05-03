"""P0-7: recall_citations writer (linkage between turn and surfaced memory).

Resolves BLOCKER #2 from plan self-audit: Phase 2 v0's
MostCitedBelowMedian/1 engine ranks memories by mean turn_score across
their citations. Without this linkage table the engine cannot
distinguish 'memory M was actually surfaced in turn T' from 'memory M
shares a session_id with turn T.'
"""
from __future__ import annotations

from opencomputer.agent.recall_citations import (
    CitationWrite,
    RecallCitationsWriter,
)
from opencomputer.agent.state import SessionDB


def _db(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    db.create_session("sess_1", platform="cli", model="opus", cwd=str(tmp_path))
    return db


def test_record_writes_episodic_row(tmp_path):
    db = _db(tmp_path)
    w = RecallCitationsWriter(db)
    cid = w.record(
        CitationWrite(
            session_id="sess_1",
            turn_index=0,
            episodic_event_id="ep1",
            candidate_kind="episodic",
            candidate_text_id=None,
            bm25_score=-3.5,
            adjusted_score=-3.0,
        )
    )
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT episodic_event_id, candidate_kind, bm25_score, "
            "adjusted_score FROM recall_citations WHERE id = ?",
            (cid,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["episodic_event_id"] == "ep1"
    assert rows[0]["candidate_kind"] == "episodic"
    assert abs(rows[0]["bm25_score"] - (-3.5)) < 1e-9
    assert abs(rows[0]["adjusted_score"] - (-3.0)) < 1e-9


def test_record_for_message_hit_with_null_episodic(tmp_path):
    db = _db(tmp_path)
    w = RecallCitationsWriter(db)
    w.record(
        CitationWrite(
            session_id="sess_1",
            turn_index=1,
            episodic_event_id=None,
            candidate_kind="message",
            candidate_text_id="abc@123",
            bm25_score=-2.1,
            adjusted_score=-2.1,
        )
    )
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT episodic_event_id, candidate_text_id, candidate_kind "
            "FROM recall_citations WHERE candidate_kind='message'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["episodic_event_id"] is None
    assert rows[0]["candidate_text_id"] == "abc@123"


def test_record_returns_uuid(tmp_path):
    db = _db(tmp_path)
    w = RecallCitationsWriter(db)
    cid = w.record(
        CitationWrite(
            session_id="sess_1",
            turn_index=0,
            episodic_event_id="ep_x",
            candidate_kind="episodic",
            candidate_text_id=None,
            bm25_score=None,
            adjusted_score=None,
        )
    )
    assert isinstance(cid, str)
    assert len(cid) == 36


def test_record_many_citations_same_turn(tmp_path):
    db = _db(tmp_path)
    w = RecallCitationsWriter(db)
    for i in range(5):
        w.record(
            CitationWrite(
                session_id="sess_1",
                turn_index=0,
                episodic_event_id=f"ep_{i}",
                candidate_kind="episodic",
                candidate_text_id=None,
                bm25_score=-float(i + 1),
                adjusted_score=-float(i + 1),
            )
        )
    with db._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM recall_citations "
            "WHERE session_id = 'sess_1' AND turn_index = 0"
        ).fetchone()[0]
    assert n == 5
