"""Loop banner formatter — achieved / continue / pause_budget.

Pure-function tests; no console / no AgentLoop. The formatter is wired
onto AgentLoop.goal_banner_callback in cli.py and called by
_maybe_continue_goal. This file exercises the formatting contract; the
loop integration is tested in tests/test_agent_loop_goal.py.
"""
from __future__ import annotations

from opencomputer.agent.goal import GoalState, JudgeVerdict
from opencomputer.cli_ui.goal_banner import format_banner


def test_format_banner_continue():
    g = GoalState(text="create 4 files", turns_used=1, budget=20)
    v = JudgeVerdict(done=False, reason="1 of 4 done")
    text = format_banner(kind="continue", verdict=v, goal=g)
    assert "↻" in text
    assert "1/20" in text
    assert "1 of 4 done" in text
    assert "Continuing toward goal" in text


def test_format_banner_achieved():
    g = GoalState(text="x", turns_used=4, budget=20)
    v = JudgeVerdict(done=True, reason="all 4 created")
    text = format_banner(kind="achieved", verdict=v, goal=g)
    assert "✓" in text
    assert "Goal achieved" in text
    assert "all 4 created" in text


def test_format_banner_pause_budget():
    g = GoalState(text="x", turns_used=20, budget=20)
    v = JudgeVerdict(done=False, reason="not done; budget exhausted")
    text = format_banner(kind="pause_budget", verdict=v, goal=g)
    assert "⏸" in text
    assert "20/20" in text
    assert "/goal resume" in text
    assert "/goal clear" in text


def test_format_banner_unknown_kind_falls_back_to_continue():
    """Defensive: unknown kind shouldn't crash — render as continue."""
    g = GoalState(text="x", turns_used=2, budget=10)
    v = JudgeVerdict(done=False, reason="ok")
    text = format_banner(kind="garbage_value", verdict=v, goal=g)
    assert "↻" in text  # continue rendering
    assert "2/10" in text
