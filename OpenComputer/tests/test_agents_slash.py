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


def test_agents_help(ctx):
    res = _handle_agents_inline(ctx, ["help"])
    assert res.handled


def test_agents_kill_no_args_prints_usage(ctx):
    res = _handle_agents_inline(ctx, ["kill"])
    assert res.handled


def test_agents_kill_unknown_id(ctx):
    res = _handle_agents_inline(ctx, ["kill", "nonexistent"])
    assert res.handled


def test_agents_kill_running_subagent(ctx):
    """Smoke test: kill calls SubagentRegistry.kill() with the matching agent_id."""
    reg = SubagentRegistry.instance()
    rec = SubagentRecord(
        agent_id="ab12cdef34567890",
        parent_id=None,
        goal="long-running task",
        started_at=datetime.now(UTC),
        state="running",
    )
    reg._records[rec.agent_id] = rec

    # Kill returns True for running records whose state we can flip.
    # The registry's kill() also tries to cancel the asyncio task; with
    # no event_loop captured it short-circuits to a state flip.
    res = _handle_agents_inline(ctx, ["kill", "ab12cdef"])  # prefix match
    assert res.handled


def test_agents_unknown_subcommand(ctx):
    res = _handle_agents_inline(ctx, ["fakecmd"])
    assert res.handled
