"""Tests for opencomputer.evolution.monitor.MonitorDashboard."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from opencomputer.evolution.monitor import MonitorDashboard, _iter_reward_rows
from opencomputer.evolution.storage import (
    apply_pending,
    insert_record,
    record_reflection,
    record_skill_invocation,
    update_reward,
)
from opencomputer.evolution.trajectory import TrajectoryEvent, TrajectoryRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    session_id: str = "s",
    started_at: float = 0.0,
    ended_at: float | None = None,
) -> TrajectoryRecord:
    ev = TrajectoryEvent(
        session_id=session_id,
        message_id=None,
        action_type="tool_call",
        tool_name="Read",
        outcome="success",
        timestamp=started_at,
        metadata={},
    )
    return TrajectoryRecord(
        id=None,
        session_id=session_id,
        schema_version=1,
        started_at=started_at,
        ended_at=ended_at,
        events=(ev,),
        completion_flag=True,
    )


def _make_skill(skills_dir: Path, slug: str) -> None:
    skill_dir = skills_dir / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nslug: {slug}\ndescription: Test skill\n---\n",
        encoding="utf-8",
    )


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """Set OPENCOMPUTER_HOME to tmp_path and pre-migrate the DB."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    evo_dir = tmp_path / "evolution"
    evo_dir.mkdir(parents=True, exist_ok=True)
    db_path = evo_dir / "trajectory.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)
    yield tmp_path, conn
    conn.close()


# ---------------------------------------------------------------------------
# Empty home — zero counts
# ---------------------------------------------------------------------------


def test_snapshot_empty_home_zero_counts(isolated_env):
    _tmp_path, _conn = isolated_env
    dash = MonitorDashboard()
    snap = dash.snapshot()
    assert snap.total_reflections == 0
    assert snap.last_reflection_at is None
    assert snap.synthesized_skills == []
    assert snap.atrophied_count == 0
    assert snap.avg_reward_last_30 is None
    assert snap.avg_reward_lifetime is None


# ---------------------------------------------------------------------------
# Reflections count
# ---------------------------------------------------------------------------


def test_snapshot_counts_reflections(isolated_env):
    _tmp_path, conn = isolated_env
    record_reflection(
        window_size=10, records_count=5, insights_count=2, records_hash="h1", conn=conn
    )
    record_reflection(
        window_size=20, records_count=10, insights_count=3, records_hash="h2", conn=conn
    )
    snap = MonitorDashboard().snapshot()
    assert snap.total_reflections == 2


def test_snapshot_last_reflection_at_is_newest(isolated_env):
    _tmp_path, conn = isolated_env
    base = time.time()
    record_reflection(
        window_size=10, records_count=5, insights_count=0, records_hash="h1",
        invoked_at=base, conn=conn
    )
    record_reflection(
        window_size=10, records_count=5, insights_count=0, records_hash="h2",
        invoked_at=base + 100, conn=conn
    )
    snap = MonitorDashboard().snapshot()
    assert snap.last_reflection_at == pytest.approx(base + 100)


# ---------------------------------------------------------------------------
# Skills — atrophy detection
# ---------------------------------------------------------------------------


def test_skill_with_no_invocations_is_atrophied(isolated_env):
    tmp_path, conn = isolated_env
    skills_dir = tmp_path / "evolution" / "skills"
    _make_skill(skills_dir, "orphan-skill")

    snap = MonitorDashboard(atrophy_days=60).snapshot()
    assert len(snap.synthesized_skills) == 1
    assert snap.synthesized_skills[0].is_atrophied is True
    assert snap.atrophied_count == 1


def test_skill_recently_invoked_is_active(isolated_env):
    tmp_path, conn = isolated_env
    skills_dir = tmp_path / "evolution" / "skills"
    _make_skill(skills_dir, "active-skill")

    # Record invocation right now
    record_skill_invocation("active-skill", invoked_at=time.time(), conn=conn)

    snap = MonitorDashboard(atrophy_days=60).snapshot(now=time.time())
    skill = next(s for s in snap.synthesized_skills if s.slug == "active-skill")
    assert skill.is_atrophied is False


def test_skill_old_invocation_is_atrophied(isolated_env):
    tmp_path, conn = isolated_env
    skills_dir = tmp_path / "evolution" / "skills"
    _make_skill(skills_dir, "old-skill")

    # Invocation 100 days ago
    old_ts = time.time() - (100 * 24 * 3600)
    record_skill_invocation("old-skill", invoked_at=old_ts, conn=conn)

    snap = MonitorDashboard(atrophy_days=60).snapshot(now=time.time())
    skill = next(s for s in snap.synthesized_skills if s.slug == "old-skill")
    assert skill.is_atrophied is True


def test_atrophied_count_correct(isolated_env):
    tmp_path, conn = isolated_env
    skills_dir = tmp_path / "evolution" / "skills"
    _make_skill(skills_dir, "skill-a")
    _make_skill(skills_dir, "skill-b")

    now = time.time()
    # skill-a: recently invoked → active
    record_skill_invocation("skill-a", invoked_at=now - 10, conn=conn)
    # skill-b: no invocations → atrophied

    snap = MonitorDashboard(atrophy_days=60).snapshot(now=now)
    assert snap.atrophied_count == 1


def test_skill_invocation_count(isolated_env):
    tmp_path, conn = isolated_env
    skills_dir = tmp_path / "evolution" / "skills"
    _make_skill(skills_dir, "counted-skill")

    for _ in range(4):
        record_skill_invocation("counted-skill", conn=conn)

    snap = MonitorDashboard().snapshot()
    skill = next(s for s in snap.synthesized_skills if s.slug == "counted-skill")
    assert skill.invocation_count == 4


# ---------------------------------------------------------------------------
# Reward averages
# ---------------------------------------------------------------------------


def test_avg_reward_lifetime_no_records(isolated_env):
    snap = MonitorDashboard().snapshot()
    assert snap.avg_reward_lifetime is None


def test_avg_reward_lifetime(isolated_env):
    _tmp_path, conn = isolated_env
    now = time.time()
    rec1 = insert_record(_make_record(ended_at=now - 100), conn=conn)
    rec2 = insert_record(_make_record(ended_at=now - 200), conn=conn)
    update_reward(rec1, 0.8, conn=conn)
    update_reward(rec2, 0.6, conn=conn)

    snap = MonitorDashboard().snapshot()
    assert snap.avg_reward_lifetime == pytest.approx(0.7, abs=1e-6)


def test_avg_reward_30d_excludes_old_records(isolated_env):
    _tmp_path, conn = isolated_env
    now = time.time()
    # Within 30 days
    rec_new = insert_record(_make_record(ended_at=now - 10), conn=conn)
    # Older than 30 days
    rec_old = insert_record(_make_record(ended_at=now - 40 * 24 * 3600), conn=conn)
    update_reward(rec_new, 0.9, conn=conn)
    update_reward(rec_old, 0.1, conn=conn)

    snap = MonitorDashboard().snapshot(now=now)
    # 30d avg should only include rec_new
    assert snap.avg_reward_last_30 == pytest.approx(0.9, abs=1e-6)


def test_iter_reward_rows_lifetime_returns_all(isolated_env):
    _tmp_path, conn = isolated_env
    now = time.time()
    rec1 = insert_record(_make_record(ended_at=now - 10), conn=conn)
    rec2 = insert_record(_make_record(ended_at=now - 5000), conn=conn)
    update_reward(rec1, 0.5, conn=conn)
    update_reward(rec2, 0.3, conn=conn)

    scores = _iter_reward_rows(window_start_ts=None, conn=conn)
    assert sorted(scores) == pytest.approx([0.3, 0.5], abs=1e-6)
