"""Hermes parity: /agents — read-only subagent tree."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.subagent_registry import SubagentRecord, SubagentRegistry
from opencomputer.cli_ui.slash_handlers import SlashContext, _handle_agents_inline


@pytest.fixture
def ctx():
    console = MagicMock()
    return SlashContext(
        console=console,
        session_id="s1",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=list,
    )


@pytest.fixture(autouse=True)
def reset_registry():
    SubagentRegistry.instance().reset()
    yield
    SubagentRegistry.instance().reset()


def test_agents_empty(ctx):
    res = _handle_agents_inline(ctx, [])
    assert res.handled
    ctx.console.print.assert_called()


def test_agents_renders_running(ctx):
    reg = SubagentRegistry.instance()
    reg._records["a1"] = SubagentRecord(
        agent_id="a1",
        parent_id=None,
        goal="Research X",
        started_at=datetime.now(UTC),
        state="running",
    )
    res = _handle_agents_inline(ctx, [])
    assert res.handled
    printed = " ".join(str(c) for c in ctx.console.print.call_args_list)
    assert "Research X" in printed
    assert "running" in printed.lower()


def test_agents_renders_finished(ctx):
    reg = SubagentRegistry.instance()
    started = datetime.now(UTC)
    reg._records["a1"] = SubagentRecord(
        agent_id="a1",
        parent_id=None,
        goal="Done thing",
        started_at=started,
        ended_at=started,
        state="completed",
    )
    res = _handle_agents_inline(ctx, [])
    assert res.handled
    printed = " ".join(str(c) for c in ctx.console.print.call_args_list)
    assert "Done thing" in printed
    assert "completed" in printed.lower()
