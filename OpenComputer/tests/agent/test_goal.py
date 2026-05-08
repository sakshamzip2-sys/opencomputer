"""Tests for opencomputer.agent.goal — persistent cross-turn goals (Ralph loop).

Covers:
- GoalState dataclass shape (incl. v2 ``last_judge_reason``)
- SessionDB.set_session_goal / get_session_goal / update_session_goal /
  clear_session_goal — schema v11+v14 columns on the sessions table
- judge_goal strict-JSON parsing + fail-open semantics (v2)
- _call_judge_model goal_judge config routing (v2)

Continuation-loop integration in agent/loop.py is exercised in
tests/test_agent_loop_goal.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.goal import (
    DEFAULT_BUDGET,
    GoalState,
    JudgeVerdict,
    build_continuation_prompt,
    judge_goal,
)
from opencomputer.agent.state import SessionDB


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "test_state.db")


def _new_sid(db: SessionDB) -> str:
    import uuid

    sid = str(uuid.uuid4())
    db.create_session(sid, platform="cli")
    return sid


# ─── GoalState ──────────────────────────────────────────────────────────


def test_goal_state_defaults():
    g = GoalState(text="ship the wave-5 PR")
    assert g.text == "ship the wave-5 PR"
    assert g.active is True
    assert g.turns_used == 0
    assert g.budget == DEFAULT_BUDGET == 20
    assert g.last_judge_reason is None


def test_goal_state_budget_exhausted():
    g = GoalState(text="x", budget=3, turns_used=3)
    assert g.budget_exhausted() is True
    assert g.should_continue() is False

    g2 = GoalState(text="x", budget=3, turns_used=2)
    assert g2.budget_exhausted() is False
    assert g2.should_continue() is True

    paused = GoalState(text="x", active=False, budget=3, turns_used=10)
    assert paused.budget_exhausted() is False  # paused goals never "exhausted"
    assert paused.should_continue() is False


# ─── SessionDB CRUD ─────────────────────────────────────────────────────


def test_set_get_clear_roundtrip(db: SessionDB):
    sid = _new_sid(db)
    assert db.get_session_goal(sid) is None

    db.set_session_goal(sid, text="ship the feature", budget=15)
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.text == "ship the feature"
    assert g.budget == 15
    assert g.active is True
    assert g.turns_used == 0

    db.clear_session_goal(sid)
    assert db.get_session_goal(sid) is None


def test_update_session_goal_partial(db: SessionDB):
    sid = _new_sid(db)
    db.set_session_goal(sid, text="x")

    db.update_session_goal(sid, turns_used=3)
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.turns_used == 3
    assert g.text == "x"  # text unchanged
    assert g.active is True


def test_pause_resume_resets_turn_counter(db: SessionDB):
    sid = _new_sid(db)
    db.set_session_goal(sid, text="x")
    db.update_session_goal(sid, turns_used=5)

    db.update_session_goal(sid, active=False)
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.active is False
    assert g.turns_used == 5

    db.update_session_goal(sid, active=True, turns_used=0)
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.active is True
    assert g.turns_used == 0


def test_should_continue_logic(db: SessionDB):
    sid = _new_sid(db)
    assert db.get_session_goal(sid) is None

    db.set_session_goal(sid, text="x", budget=3)
    g = db.get_session_goal(sid)
    assert g is not None and g.should_continue() is True

    db.update_session_goal(sid, turns_used=3)
    g = db.get_session_goal(sid)
    assert g is not None and g.should_continue() is False

    db.update_session_goal(sid, turns_used=0, active=False)
    g = db.get_session_goal(sid)
    assert g is not None and g.should_continue() is False


def test_build_continuation_prompt_includes_goal_text():
    out = build_continuation_prompt("ship feature X")
    assert "ship feature X" in out


# ─── judge_goal — strict JSON, fail-open ────────────────────────────────


@pytest.mark.asyncio
async def test_judge_goal_parses_done_true(monkeypatch):
    async def fake(prompt: str) -> str:
        return '{"done": true, "reason": "all four files exist"}'

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", fake)
    v = await judge_goal(goal_text="create 4 files", last_response="done")
    assert isinstance(v, JudgeVerdict)
    assert v.done is True
    assert v.reason == "all four files exist"


@pytest.mark.asyncio
async def test_judge_goal_parses_done_false(monkeypatch):
    async def fake(prompt: str) -> str:
        return '{"done": false, "reason": "1 of 4 done; 3 remain"}'

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", fake)
    v = await judge_goal(goal_text="x", last_response="y")
    assert v.done is False
    assert "remain" in v.reason


@pytest.mark.asyncio
async def test_judge_goal_strips_json_fence(monkeypatch):
    async def fake(prompt: str) -> str:
        return '```json\n{"done": true, "reason": "ok"}\n```'

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", fake)
    v = await judge_goal(goal_text="x", last_response="y")
    assert v.done is True
    assert v.reason == "ok"


@pytest.mark.asyncio
async def test_judge_goal_strips_bare_fence(monkeypatch):
    async def fake(prompt: str) -> str:
        return '```\n{"done": false, "reason": "no"}\n```'

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", fake)
    v = await judge_goal(goal_text="x", last_response="y")
    assert v.done is False
    assert v.reason == "no"


@pytest.mark.asyncio
async def test_judge_goal_fails_open_on_garbage(monkeypatch):
    async def fake(prompt: str) -> str:
        return "I think we're done"  # not JSON

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", fake)
    v = await judge_goal(goal_text="x", last_response="y")
    assert v.done is False
    assert "unparseable" in v.reason.lower()


@pytest.mark.asyncio
async def test_judge_goal_fails_open_on_missing_keys(monkeypatch):
    async def fake(prompt: str) -> str:
        return '{"complete": true}'  # wrong key shape

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", fake)
    v = await judge_goal(goal_text="x", last_response="y")
    assert v.done is False
    assert "unparseable" in v.reason.lower()


@pytest.mark.asyncio
async def test_judge_goal_fails_open_on_network_error(monkeypatch):
    async def boom(prompt: str) -> str:
        raise RuntimeError("connection reset")

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", boom)
    v = await judge_goal(goal_text="x", last_response="y")
    assert v.done is False
    assert "RuntimeError" in v.reason


@pytest.mark.asyncio
async def test_judge_goal_empty_response(monkeypatch):
    async def fake(prompt: str) -> str:
        return ""

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", fake)
    v = await judge_goal(goal_text="x", last_response="y")
    assert v.done is False


@pytest.mark.asyncio
async def test_judge_goal_empty_assistant_response_skips_judge():
    """No assistant text → don't even call the judge."""
    v = await judge_goal(goal_text="x", last_response="")
    assert v.done is False
    assert "empty" in v.reason.lower()


@pytest.mark.asyncio
async def test_judge_goal_done_true_no_reason_synthesizes(monkeypatch):
    async def fake(prompt: str) -> str:
        return '{"done": true, "reason": ""}'

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", fake)
    v = await judge_goal(goal_text="x", last_response="y")
    assert v.done is True
    assert v.reason  # non-empty fallback
