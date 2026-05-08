"""``oc agents running / kill / history`` CLI (Hermes parity, 2026-05-08)."""

from __future__ import annotations

from datetime import UTC, datetime

from typer.testing import CliRunner

from opencomputer.agent.subagent_registry import SubagentRegistry
from opencomputer.cli import app

runner = CliRunner()


def setup_function(_):
    SubagentRegistry.instance().reset()


def teardown_function(_):
    SubagentRegistry.instance().reset()


def test_agents_running_empty_says_so():
    result = runner.invoke(app, ["agents", "running"])
    assert result.exit_code == 0
    assert "no running" in result.output


def test_agents_running_shows_active():
    rec = SubagentRegistry.instance().register(parent_id=None, goal="my running task")
    result = runner.invoke(app, ["agents", "running"])
    assert result.exit_code == 0
    # Rich tables can wrap long ids — check the prefix is present.
    assert rec.agent_id[:8] in result.output


def test_agents_kill_running_exits_zero():
    rec = SubagentRegistry.instance().register(parent_id=None, goal="x")
    result = runner.invoke(app, ["agents", "kill", rec.agent_id])
    assert result.exit_code == 0
    assert "killed" in result.output


def test_agents_kill_unknown_exits_one():
    result = runner.invoke(app, ["agents", "kill", "no-such-agent"])
    assert result.exit_code == 1


def test_agents_history_empty_says_so():
    result = runner.invoke(app, ["agents", "history"])
    assert result.exit_code == 0
    assert "no completed" in result.output


def test_agents_history_shows_completed():
    reg = SubagentRegistry.instance()
    rec = reg.register(parent_id=None, goal="finished task")
    reg.update(rec.agent_id, state="completed", ended_at=datetime.now(UTC))
    result = runner.invoke(app, ["agents", "history"])
    assert result.exit_code == 0
    assert rec.agent_id[:8] in result.output


def test_agents_history_respects_limit():
    reg = SubagentRegistry.instance()
    for i in range(5):
        rec = reg.register(parent_id=None, goal=f"task-{i}")
        reg.update(rec.agent_id, state="completed", ended_at=datetime.now(UTC))
    result = runner.invoke(app, ["agents", "history", "--limit", "2"])
    assert result.exit_code == 0
    # Title should include "(last 2)"
    assert "(last 2)" in result.output


def test_agents_list_templates_still_works():
    """Existing `oc agents list` (template management) must keep working
    after we added the runtime commands. Sanity check."""
    result = runner.invoke(app, ["agents", "list"])
    # Even if no templates are discovered in the test env, the command
    # must exit 0 with a friendly "no templates found" message.
    assert result.exit_code == 0
