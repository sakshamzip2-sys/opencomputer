"""Tests for opencomputer.agent.goal — persistent cross-turn goals (Ralph loop).

Covers:
- GoalState dataclass shape
- SessionDB.set_session_goal / get_session_goal / update_session_goal /
  clear_session_goal — schema v11 columns on the sessions table
- judge_satisfied fail-open semantics

Continuation-loop integration in agent/loop.py is deferred to a follow-up;
these tests exercise the storage + state API only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.goal import (
    DEFAULT_BUDGET,
    GoalState,
    build_continuation_prompt,
    judge_satisfied,
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


def test_goal_state_defaults():
    g = GoalState(text="ship the wave-5 PR")
    assert g.text == "ship the wave-5 PR"
    assert g.active is True
    assert g.turns_used == 0
    assert g.budget == DEFAULT_BUDGET == 20


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

    # Pause
    db.update_session_goal(sid, active=False)
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.active is False
    assert g.turns_used == 5  # not reset by pause

    # Resume — explicitly resets turns_used to 0
    db.update_session_goal(sid, active=True, turns_used=0)
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.active is True
    assert g.turns_used == 0


def test_should_continue_logic(db: SessionDB):
    sid = _new_sid(db)
    # No goal → False
    assert db.get_session_goal(sid) is None

    db.set_session_goal(sid, text="x", budget=3)
    g = db.get_session_goal(sid)
    assert g is not None and g.should_continue() is True

    # Budget exhausted
    db.update_session_goal(sid, turns_used=3)
    g = db.get_session_goal(sid)
    assert g is not None and g.should_continue() is False

    # Reset budget but pause
    db.update_session_goal(sid, turns_used=0, active=False)
    g = db.get_session_goal(sid)
    assert g is not None and g.should_continue() is False


def test_build_continuation_prompt_includes_goal_text():
    out = build_continuation_prompt("ship feature X")
    assert "ship feature X" in out


@pytest.mark.asyncio
async def test_judge_satisfied_fails_open_on_exception(monkeypatch):
    """Judge call failure must not block — return False (= 'continue loop')."""

    async def boom(*a, **kw):
        raise RuntimeError("model down")

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", boom)
    result = await judge_satisfied(goal_text="x", last_response="y")
    assert result is False


@pytest.mark.asyncio
async def test_judge_satisfied_empty_response_returns_false():
    result = await judge_satisfied(goal_text="x", last_response="")
    assert result is False


@pytest.mark.asyncio
async def test_judge_satisfied_recognizes_satisfied(monkeypatch):
    async def reply(prompt: str) -> str:
        return "SATISFIED"

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", reply)
    assert await judge_satisfied(goal_text="x", last_response="done") is True


@pytest.mark.asyncio
async def test_judge_satisfied_treats_not_satisfied_as_false(monkeypatch):
    async def reply(prompt: str) -> str:
        return "NOT_SATISFIED"

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", reply)
    assert await judge_satisfied(goal_text="x", last_response="meh") is False
