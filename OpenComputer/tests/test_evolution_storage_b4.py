"""Tests for B4 additions to opencomputer.evolution.storage.

Covers: record_reflection, list_reflections, record_skill_invocation,
list_skill_invocations, record_prompt_proposal, list_prompt_proposals
(with status filter), update_prompt_proposal_status, and migration
table creation.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from opencomputer.evolution.storage import (
    apply_pending,
    list_prompt_proposals,
    list_reflections,
    list_skill_invocations,
    record_prompt_proposal,
    record_reflection,
    record_skill_invocation,
    update_prompt_proposal_status,
)

# ---------------------------------------------------------------------------
# Shared fixture — fresh in-memory DB with all migrations applied
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Migration: B4 tables exist after apply_pending
# ---------------------------------------------------------------------------


def test_b4_migration_creates_reflections_table():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "reflections" in tables
    assert "skill_invocations" in tables
    assert "prompt_proposals" in tables
    conn.close()


def test_b4_migration_schema_version_is_2():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    # Migration 003 (T2.4 cache-warning column) is now applied after 002;
    # assert at least 2 (B4) so future migrations don't break this check.
    assert row[0] >= 2
    conn.close()


# ---------------------------------------------------------------------------
# record_reflection / list_reflections
# ---------------------------------------------------------------------------


def test_record_reflection_returns_positive_id(db):
    rid = record_reflection(
        window_size=30,
        records_count=10,
        insights_count=3,
        records_hash="abc123",
        conn=db,
    )
    assert isinstance(rid, int)
    assert rid > 0


def test_record_reflection_row_values(db):
    ts = time.time()
    record_reflection(
        window_size=20,
        records_count=5,
        insights_count=2,
        records_hash="deadbeef",
        cache_hit=True,
        invoked_at=ts,
        conn=db,
    )
    rows = list_reflections(conn=db)
    assert len(rows) == 1
    row = rows[0]
    assert row["window_size"] == 20
    assert row["records_count"] == 5
    assert row["insights_count"] == 2
    assert row["records_hash"] == "deadbeef"
    assert row["cache_hit"] == 1
    assert row["invoked_at"] == pytest.approx(ts)


def test_list_reflections_ordered_newest_first(db):
    base = time.time()
    for i in range(3):
        record_reflection(
            window_size=i,
            records_count=i,
            insights_count=0,
            records_hash=f"hash{i}",
            invoked_at=base + i,
            conn=db,
        )
    rows = list_reflections(conn=db)
    assert rows[0]["invoked_at"] > rows[1]["invoked_at"]
    assert rows[1]["invoked_at"] > rows[2]["invoked_at"]


def test_list_reflections_respects_limit(db):
    for i in range(5):
        record_reflection(
            window_size=1,
            records_count=1,
            insights_count=0,
            records_hash=f"h{i}",
            conn=db,
        )
    assert len(list_reflections(limit=2, conn=db)) == 2


# ---------------------------------------------------------------------------
# record_skill_invocation / list_skill_invocations
# ---------------------------------------------------------------------------


def test_record_skill_invocation_returns_id(db):
    sid = record_skill_invocation("my-skill", conn=db)
    assert isinstance(sid, int)
    assert sid > 0


def test_record_skill_invocation_row_values(db):
    ts = time.time()
    record_skill_invocation("test-slug", invoked_at=ts, source="cli_promote", conn=db)
    rows = list_skill_invocations(slug="test-slug", conn=db)
    assert len(rows) == 1
    row = rows[0]
    assert row["slug"] == "test-slug"
    assert row["invoked_at"] == pytest.approx(ts)
    assert row["source"] == "cli_promote"


def test_list_skill_invocations_filters_by_slug(db):
    record_skill_invocation("skill-a", conn=db)
    record_skill_invocation("skill-b", conn=db)
    record_skill_invocation("skill-a", conn=db)
    rows = list_skill_invocations(slug="skill-a", conn=db)
    assert len(rows) == 2
    for r in rows:
        assert r["slug"] == "skill-a"


def test_list_skill_invocations_filters_by_since_ts(db):
    base = time.time()
    record_skill_invocation("s", invoked_at=base - 1000, conn=db)
    record_skill_invocation("s", invoked_at=base + 10, conn=db)
    rows = list_skill_invocations(slug="s", since_ts=base, conn=db)
    assert len(rows) == 1
    assert rows[0]["invoked_at"] == pytest.approx(base + 10)


def test_list_skill_invocations_ordered_newest_first(db):
    base = time.time()
    record_skill_invocation("s", invoked_at=base, conn=db)
    record_skill_invocation("s", invoked_at=base + 5, conn=db)
    rows = list_skill_invocations(slug="s", conn=db)
    assert rows[0]["invoked_at"] > rows[1]["invoked_at"]


# ---------------------------------------------------------------------------
# record_prompt_proposal / list_prompt_proposals / update_prompt_proposal_status
# ---------------------------------------------------------------------------


_SAMPLE_INSIGHT_JSON = '{"observation":"o","evidence_refs":[],"action_type":"edit_prompt","payload":{},"confidence":0.9}'


def test_record_prompt_proposal_returns_id(db):
    pid = record_prompt_proposal(
        target="system",
        diff_hint="Add a note about X",
        insight_json=_SAMPLE_INSIGHT_JSON,
        conn=db,
    )
    assert isinstance(pid, int)
    assert pid > 0


def test_record_prompt_proposal_default_status_pending(db):
    pid = record_prompt_proposal(
        target="tool_spec",
        diff_hint="Change wording",
        insight_json=_SAMPLE_INSIGHT_JSON,
        conn=db,
    )
    rows = list_prompt_proposals(conn=db)
    matching = [r for r in rows if r["id"] == pid]
    assert len(matching) == 1
    assert matching[0]["status"] == "pending"


def test_list_prompt_proposals_filter_by_status(db):
    pid1 = record_prompt_proposal(
        target="system", diff_hint="hint1", insight_json=_SAMPLE_INSIGHT_JSON, conn=db
    )
    pid2 = record_prompt_proposal(
        target="system", diff_hint="hint2", insight_json=_SAMPLE_INSIGHT_JSON, conn=db
    )
    update_prompt_proposal_status(proposal_id=pid2, status="applied", conn=db)

    pending = list_prompt_proposals(status="pending", conn=db)
    applied = list_prompt_proposals(status="applied", conn=db)

    assert all(r["status"] == "pending" for r in pending)
    assert all(r["status"] == "applied" for r in applied)
    pending_ids = [r["id"] for r in pending]
    assert pid1 in pending_ids
    assert pid2 not in pending_ids


def test_update_prompt_proposal_status_applied(db):
    pid = record_prompt_proposal(
        target="system", diff_hint="hint", insight_json=_SAMPLE_INSIGHT_JSON, conn=db
    )
    ts = time.time()
    update_prompt_proposal_status(
        proposal_id=pid, status="applied", reason="looks good", decided_at=ts, conn=db
    )
    rows = list_prompt_proposals(conn=db)
    row = next(r for r in rows if r["id"] == pid)
    assert row["status"] == "applied"
    assert row["decided_reason"] == "looks good"
    assert row["decided_at"] == pytest.approx(ts)


def test_update_prompt_proposal_status_rejected(db):
    pid = record_prompt_proposal(
        target="tool_spec", diff_hint="nope", insight_json=_SAMPLE_INSIGHT_JSON, conn=db
    )
    update_prompt_proposal_status(proposal_id=pid, status="rejected", conn=db)
    rows = list_prompt_proposals(conn=db)
    row = next(r for r in rows if r["id"] == pid)
    assert row["status"] == "rejected"


def test_update_prompt_proposal_status_invalid_raises(db):
    pid = record_prompt_proposal(
        target="system", diff_hint="h", insight_json=_SAMPLE_INSIGHT_JSON, conn=db
    )
    with pytest.raises(ValueError, match="'applied' or 'rejected'"):
        update_prompt_proposal_status(proposal_id=pid, status="bogus", conn=db)
