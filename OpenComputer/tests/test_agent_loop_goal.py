"""_maybe_continue_goal — Ralph loop continuation gate (v2).

Spec: docs/superpowers/specs/2026-05-08-kanban-goals-v2-design.md §3 Gap A/B.

Strategy: bypass the AgentLoop constructor (it pulls in providers, hooks,
caches…) by constructing the instance via ``__new__`` and attaching only
the attributes ``_maybe_continue_goal`` actually touches: ``db`` and
optional ``goal_banner_callback``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.goal import JudgeVerdict
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB


def _bare_loop(db: SessionDB) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.db = db
    return loop


@pytest.mark.asyncio
async def test_maybe_continue_goal_persists_reason_on_continue(
    monkeypatch, tmp_path: Path,
):
    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_a"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)
    db.set_session_goal(sid, text="create 4 files", budget=20)

    async def fake_judge(*, goal_text, last_response):
        return JudgeVerdict(done=False, reason="2 of 4 done")

    monkeypatch.setattr("opencomputer.agent.goal.judge_goal", fake_judge)

    loop = _bare_loop(db)
    cont = await loop._maybe_continue_goal(sid, "Did 2 files.")
    assert cont is not None  # continuation prompt returned
    assert "create 4 files" in cont

    g = db.get_session_goal(sid)
    assert g is not None
    assert g.turns_used == 1
    assert g.last_judge_reason == "2 of 4 done"


@pytest.mark.asyncio
async def test_maybe_continue_goal_clears_goal_on_done(
    monkeypatch, tmp_path: Path,
):
    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_b"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)
    db.set_session_goal(sid, text="x", budget=20)

    async def fake_judge(*, goal_text, last_response):
        return JudgeVerdict(done=True, reason="all 4 created")

    monkeypatch.setattr("opencomputer.agent.goal.judge_goal", fake_judge)

    loop = _bare_loop(db)
    cont = await loop._maybe_continue_goal(sid, "Done.")
    assert cont is None  # no continuation; loop should exit normally

    g = db.get_session_goal(sid)
    assert g is None  # cleared


@pytest.mark.asyncio
async def test_maybe_continue_goal_budget_exhausted_pauses(
    monkeypatch, tmp_path: Path,
):
    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_c"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)
    db.set_session_goal(sid, text="x", budget=2)
    # Use up 1 of 2 turns so the next continue tips over to budget=2.
    db.update_session_goal(sid, turns_used=1)

    async def fake_judge(*, goal_text, last_response):
        return JudgeVerdict(done=False, reason="not yet")

    monkeypatch.setattr("opencomputer.agent.goal.judge_goal", fake_judge)

    loop = _bare_loop(db)
    cont = await loop._maybe_continue_goal(sid, "Some progress.")
    assert cont is None  # budget hit → no continuation prompt

    g = db.get_session_goal(sid)
    assert g is not None
    assert g.turns_used == 2  # bumped, even though we won't continue
    assert g.last_judge_reason == "not yet"


@pytest.mark.asyncio
async def test_maybe_continue_goal_fires_banners(monkeypatch, tmp_path: Path):
    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_d"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)
    db.set_session_goal(sid, text="x", budget=5)

    async def fake_judge(*, goal_text, last_response):
        return JudgeVerdict(done=False, reason="ongoing")

    monkeypatch.setattr("opencomputer.agent.goal.judge_goal", fake_judge)

    fired: list[dict] = []

    def cb(*, session_id, kind, verdict, goal):
        fired.append(
            {"sid": session_id, "kind": kind, "reason": verdict.reason},
        )

    loop = _bare_loop(db)
    loop.goal_banner_callback = cb
    await loop._maybe_continue_goal(sid, "step")

    assert len(fired) == 1
    assert fired[0]["kind"] == "continue"
    assert fired[0]["reason"] == "ongoing"
    assert fired[0]["sid"] == sid


@pytest.mark.asyncio
async def test_maybe_continue_goal_swallows_banner_errors(
    monkeypatch, tmp_path: Path,
):
    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_e"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)
    db.set_session_goal(sid, text="x", budget=5)

    async def fake_judge(*, goal_text, last_response):
        return JudgeVerdict(done=True, reason="ok")

    monkeypatch.setattr("opencomputer.agent.goal.judge_goal", fake_judge)

    def crash(**kw):
        raise RuntimeError("banner blew up")

    loop = _bare_loop(db)
    loop.goal_banner_callback = crash
    cont = await loop._maybe_continue_goal(sid, "done")
    assert cont is None
    # Goal still cleared despite banner error
    assert db.get_session_goal(sid) is None


@pytest.mark.asyncio
async def test_maybe_continue_goal_no_op_without_goal(tmp_path: Path):
    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_f"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)

    loop = _bare_loop(db)
    assert await loop._maybe_continue_goal(sid, "anything") is None


@pytest.mark.asyncio
async def test_maybe_continue_goal_no_op_when_paused(tmp_path: Path):
    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_g"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)
    db.set_session_goal(sid, text="x", budget=5)
    db.update_session_goal(sid, active=False)

    loop = _bare_loop(db)
    assert await loop._maybe_continue_goal(sid, "anything") is None
