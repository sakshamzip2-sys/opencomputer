"""Hermes spec parity (2026-05-08): cron jobs CANNOT recursively create more cron jobs.

When CronTool.execute runs inside a cron session
(runtime.custom['cron_session'] = True), mutating actions return an
error result. Read-only actions (list, get) remain allowed for
introspection.
"""
from __future__ import annotations

import pytest

from opencomputer.tools.cron_tool import CronTool
from plugin_sdk.core import ToolCall
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture(autouse=True)
def reset_runtime():
    yield
    CronTool.set_runtime(DEFAULT_RUNTIME_CONTEXT)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "pause", "resume", "trigger", "remove"])
async def test_mutating_action_blocked_in_cron_session(action):
    CronTool.set_runtime(
        RuntimeContext(custom={"cron_session": True, "cron_job_id": "j1"})
    )
    tool = CronTool()
    args = {"action": action}
    if action == "create":
        args.update({"schedule": "every 1h", "skill": "x"})
    else:
        args["job_id"] = "abc"
    call = ToolCall(id="t1", name="cron", arguments=args)
    result = await tool.execute(call)
    assert result.is_error, f"action={action} should have been blocked"
    assert "cron" in result.content.lower()
    assert "disabled" in result.content.lower() or "recursive" in result.content.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["list"])
async def test_read_only_action_allowed_in_cron_session(action):
    CronTool.set_runtime(
        RuntimeContext(custom={"cron_session": True, "cron_job_id": "j1"})
    )
    tool = CronTool()
    call = ToolCall(id="t1", name="cron", arguments={"action": action})
    result = await tool.execute(call)
    assert not result.is_error, f"action={action} should remain allowed"


@pytest.mark.asyncio
async def test_mutating_action_works_outside_cron_session():
    """No cron_session marker → normal create works."""
    CronTool.set_runtime(DEFAULT_RUNTIME_CONTEXT)
    tool = CronTool()
    call = ToolCall(
        id="t1",
        name="cron",
        arguments={"action": "create", "schedule": "every 1h", "skill": "x"},
    )
    result = await tool.execute(call)
    assert not result.is_error, result.content
