"""Tests for Ralph-loop continuation wiring — Wave 5 T2 closure.

The unit tests in test_goal.py cover the helpers (judge_satisfied,
build_continuation_prompt, GoalState). These tests cover the
``AgentLoop._maybe_continue_goal`` gate that wires those helpers into
``run_conversation``'s end-of-turn return path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.config import Config, ModelConfig, SessionConfig
from opencomputer.agent.loop import AgentLoop
from plugin_sdk.provider_contract import BaseProvider


class _StubProvider(BaseProvider):
    name = "stub"

    async def complete(self, **kwargs):
        raise NotImplementedError

    async def stream_complete(self, **kwargs):
        raise NotImplementedError


def _make_loop(tmp_path: Path) -> AgentLoop:
    cfg = Config(
        model=ModelConfig(model="anthropic:claude-opus-4-7"),
        session=SessionConfig(db_path=tmp_path / "cont.db"),
    )
    return AgentLoop(provider=_StubProvider(), config=cfg)


@pytest.mark.asyncio
async def test_no_goal_returns_none(tmp_path):
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli")
    out = await loop._maybe_continue_goal(sid, "any text")
    assert out is None


@pytest.mark.asyncio
async def test_paused_goal_returns_none(tmp_path):
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli")
    loop.db.set_session_goal(sid, text="ship the wave")
    loop.db.update_session_goal(sid, active=False)
    out = await loop._maybe_continue_goal(sid, "anything")
    assert out is None


@pytest.mark.asyncio
async def test_budget_exhausted_returns_none(tmp_path):
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli")
    loop.db.set_session_goal(sid, text="ship the wave", budget=3)
    loop.db.update_session_goal(sid, turns_used=3)
    out = await loop._maybe_continue_goal(sid, "anything")
    assert out is None


@pytest.mark.asyncio
async def test_judge_satisfied_clears_goal_returns_none(tmp_path, monkeypatch):
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli")
    loop.db.set_session_goal(sid, text="ship the wave")

    async def reply(prompt: str) -> str:
        return "SATISFIED"

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", reply)
    out = await loop._maybe_continue_goal(sid, "we shipped it")
    assert out is None
    assert loop.db.get_session_goal(sid) is None  # cleared


@pytest.mark.asyncio
async def test_judge_not_satisfied_returns_continuation(tmp_path, monkeypatch):
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli")
    loop.db.set_session_goal(sid, text="ship the wave", budget=10)

    async def reply(prompt: str) -> str:
        return "NOT_SATISFIED"

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", reply)
    out = await loop._maybe_continue_goal(sid, "still working")
    assert out is not None
    assert "ship the wave" in out
    # Counter incremented
    g = loop.db.get_session_goal(sid)
    assert g is not None
    assert g.turns_used == 1


@pytest.mark.asyncio
async def test_judge_failure_treated_as_not_satisfied(tmp_path, monkeypatch):
    """Fail-open: if the judge call raises, treat as NOT_SATISFIED so the
    loop continues toward the goal (budget is the real backstop)."""
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli")
    loop.db.set_session_goal(sid, text="goal")

    async def boom(prompt: str) -> str:
        raise RuntimeError("aux model down")

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", boom)
    out = await loop._maybe_continue_goal(sid, "any text")
    assert out is not None  # continuation prompt returned
    g = loop.db.get_session_goal(sid)
    assert g is not None and g.turns_used == 1


@pytest.mark.asyncio
async def test_continuation_increments_to_budget_then_stops(tmp_path, monkeypatch):
    """After ``budget`` continuations the gate must stop returning prompts."""
    loop = _make_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli")
    loop.db.set_session_goal(sid, text="goal", budget=2)

    async def reply(prompt: str) -> str:
        return "NOT_SATISFIED"

    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", reply)
    # 2 continuations used → 3rd call returns None (budget hit)
    assert await loop._maybe_continue_goal(sid, "x") is not None
    assert await loop._maybe_continue_goal(sid, "x") is not None
    assert await loop._maybe_continue_goal(sid, "x") is None
