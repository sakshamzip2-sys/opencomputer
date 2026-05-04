"""Integration smoke tests for ``ToolLoopGuard`` per-turn lifecycle.

The unit tests in ``test_tool_guardrails.py`` cover the guard's contract
directly. These exercise the lifecycle the way the agent loop uses it:
reset on each user turn, observe on each tool dispatch, hard-stop after
the configured threshold — so a future refactor that breaks the
reset/observe pattern fails here too.
"""

from __future__ import annotations

import pytest

from opencomputer.agent.tool_guardrails import (
    ToolLoopGuard,
    ToolLoopGuardrailError,
)


def test_loop_guard_in_agent_lifecycle():
    """Guard resets per-turn, observes calls, raises at stop_at."""
    g = ToolLoopGuard(warn_at=2, stop_at=4)
    # Turn 1: 3 calls — warn at 2nd
    for i in range(3):
        v = g.observe({"name": "bash", "arguments": {"cmd": "ls"}})
        if i == 1:
            assert v.level == "warn"
    # Reset for new turn (this is what AgentLoop.run_conversation does)
    g.reset()
    # Turn 2: hit stop_at on the 4th identical call
    with pytest.raises(ToolLoopGuardrailError):
        for _ in range(4):
            g.observe({"name": "bash", "arguments": {"cmd": "ls"}})


def test_loop_guard_tolerates_diverse_calls():
    """A turn with varied tool calls never trips the guard."""
    g = ToolLoopGuard(warn_at=3, stop_at=5)
    # 20 calls but each one different — streak resets every time.
    for i in range(20):
        v = g.observe(
            {"name": "bash", "arguments": {"cmd": f"echo {i}"}},
        )
        assert v.level == "ok"


def test_loop_guard_disable_via_config_path():
    """The disable knob short-circuits both warn and stop verdicts."""
    g = ToolLoopGuard(warn_at=2, stop_at=3, enabled=False)
    for _ in range(50):
        v = g.observe({"name": "x", "arguments": {}})
        assert v.level == "ok"
