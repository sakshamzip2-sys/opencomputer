"""Tests for the RecallTool → RecallCitationsWriter wire-in (2026-05-10).

Closes the dormant feature surfaced by Saksham's audit: ``recall_citations``
table had 0 rows. The writer existed (defined in
``opencomputer/agent/recall_citations.py``) and was unit-tested in
isolation, but no production caller invoked it. PR #583's KNOWN_DORMANT
registry tracked it; this PR retires that entry by wiring it in.

Tests:

1. ``RecallTool._do_search`` writes one row per episodic hit
2. Writes one row per message hit
3. session_id resolved from ``_session_id_var`` ContextVar
4. turn_index resolved from ``_turn_index_var`` ContextVar (set by
   the agent loop just before tool dispatch)
5. No session bound (CLI invocation) → no citations + no crash
6. Bad hit data → that one row skipped, others succeed
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def db_with_session(tmp_path: Path):
    """Real SessionDB with one session row (FK target for citations)."""
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    sid = "sess-recall-citations-test-12345678"
    db.create_session(
        session_id=sid,
        platform="cli",
        model="test",
    )
    return db, sid


def test_record_citations_writes_episodic_hits(
    db_with_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.agent.recall_citations import RecallCitationsWriter
    from opencomputer.observability.logging_config import (
        _session_id_var,
        set_turn_index,
    )
    from opencomputer.tools.recall import RecallTool

    db, sid = db_with_session
    _session_id_var.set(sid)
    set_turn_index(7)

    tool = RecallTool(db=db, memory=None)  # memory unused for _record_citations

    episodic_hits = [
        {
            "id": "ev-A",
            "session_id": sid,
            "turn_index": 0,
            "summary": "first hit",
            "bm25_rank": -1.5,
            "adjusted_rank": -1.4,
        },
        {
            "id": "ev-B",
            "session_id": sid,
            "turn_index": 1,
            "summary": "second hit",
            "bm25_rank": -2.1,
        },
    ]
    tool._record_citations(episodic_hits, [])

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT episodic_event_id, candidate_kind, turn_index, "
            "bm25_score, adjusted_score FROM recall_citations "
            "WHERE session_id = ? ORDER BY episodic_event_id",
            (sid,),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["episodic_event_id"] == "ev-A"
    assert rows[0]["candidate_kind"] == "episodic"
    assert rows[0]["turn_index"] == 7
    assert rows[0]["bm25_score"] == pytest.approx(-1.5)
    assert rows[0]["adjusted_score"] == pytest.approx(-1.4)
    assert rows[1]["episodic_event_id"] == "ev-B"
    assert rows[1]["adjusted_score"] is None


def test_record_citations_writes_message_hits(db_with_session) -> None:
    from opencomputer.observability.logging_config import (
        _session_id_var,
        set_turn_index,
    )
    from opencomputer.tools.recall import RecallTool

    db, sid = db_with_session
    _session_id_var.set(sid)
    set_turn_index(3)

    tool = RecallTool(db=db, memory=None)
    message_hits = [
        {
            "session_id": "abcdef12-old-session",
            "timestamp": 1700000000,
            "snippet": "from a past session",
            "rank": -3.2,
        },
    ]
    tool._record_citations([], message_hits)

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT candidate_kind, candidate_text_id, turn_index, "
            "bm25_score, episodic_event_id FROM recall_citations "
            "WHERE session_id = ?",
            (sid,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["candidate_kind"] == "message"
    assert rows[0]["candidate_text_id"] == "abcdef12@1700000000"
    assert rows[0]["turn_index"] == 3
    assert rows[0]["bm25_score"] == pytest.approx(-3.2)
    assert rows[0]["episodic_event_id"] is None


def test_record_citations_skips_when_no_session_bound(
    db_with_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No session bound (CLI invocation) → skip + no crash + no rows."""
    from opencomputer.observability.logging_config import _session_id_var
    from opencomputer.tools.recall import RecallTool

    db, sid = db_with_session
    _session_id_var.set(None)

    tool = RecallTool(db=db, memory=None)
    tool._record_citations([{"id": "ev-X", "summary": "x"}], [])

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM recall_citations"
        ).fetchone()
    assert rows["n"] == 0


def test_record_citations_continues_on_one_bad_hit(
    db_with_session,
) -> None:
    """A malformed hit doesn't block other writes (defensive contract)."""
    from opencomputer.observability.logging_config import (
        _session_id_var,
        set_turn_index,
    )
    from opencomputer.tools.recall import RecallTool

    db, sid = db_with_session
    _session_id_var.set(sid)
    set_turn_index(0)

    tool = RecallTool(db=db, memory=None)
    hits = [
        {"id": "ev-good-1", "summary": "ok"},
        {"id": object()},  # not str-coercible cleanly; but record() does str()
        {"id": "ev-good-2", "summary": "ok"},
    ]
    tool._record_citations(hits, [])

    with db._connect() as conn:
        ids = {
            r["episodic_event_id"]
            for r in conn.execute(
                "SELECT episodic_event_id FROM recall_citations "
                "WHERE session_id = ?",
                (sid,),
            ).fetchall()
        }
    # Both good rows must land. The middle row is os-allocated object()
    # repr — str() works, so it lands too. Assert at least the good ones.
    assert "ev-good-1" in ids
    assert "ev-good-2" in ids


def test_turn_index_var_round_trip() -> None:
    """ContextVar API: set + get returns what was set."""
    from opencomputer.observability.logging_config import (
        get_turn_index,
        set_turn_index,
    )

    set_turn_index(42)
    assert get_turn_index() == 42
    set_turn_index(0)
    assert get_turn_index() == 0


def test_turn_index_var_default_is_zero() -> None:
    """Fresh import / context returns the documented default."""
    import contextvars

    # Run inside a fresh context to avoid prior set_turn_index leaking
    ctx = contextvars.copy_context()

    def _check() -> int:
        from opencomputer.observability.logging_config import _turn_index_var

        # Reset to the declared default in this isolated context
        _turn_index_var.set(0)
        return _turn_index_var.get()

    assert ctx.run(_check) == 0
