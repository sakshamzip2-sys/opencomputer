"""CronTool emits audit-log lines for every mutating action.

Production-grade defense-in-depth: every cron job mutation routes through
``audit_log`` so an operator can reconstruct who created/paused/removed
which job. Read-only actions (list, get) do NOT emit audit lines.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from opencomputer.tools.cron_tool import CronTool
from plugin_sdk.core import ToolCall


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    yield tmp_path


@pytest.mark.asyncio
async def test_create_emits_audit_log():
    captured: list[tuple[str, dict]] = []
    with patch(
        "opencomputer.dashboard.routes._common.audit_log",
        side_effect=lambda action, **f: captured.append((action, f)),
    ):
        tool = CronTool()
        call = ToolCall(
            id="t1",
            name="cron",
            arguments={
                "action": "create",
                "schedule": "every 1h",
                "skill": "x",
                "notify": "telegram:1",
            },
        )
        result = await tool.execute(call)
        assert not result.is_error, result.content
    assert captured, "expected an audit log call"
    action, fields = captured[0]
    assert action == "cron.create"
    assert "job_id" in fields
    assert fields["source"] == "cron_tool"
    assert fields["notify"] == "telegram:1"


@pytest.mark.asyncio
async def test_pause_resume_remove_emit_audit_log():
    from opencomputer.cron.jobs import create_job
    job = create_job(schedule="every 1h", skill="x")

    captured: list[tuple[str, dict]] = []
    with patch(
        "opencomputer.dashboard.routes._common.audit_log",
        side_effect=lambda action, **f: captured.append((action, f)),
    ):
        tool = CronTool()
        for action in ("pause", "resume", "trigger", "remove"):
            call = ToolCall(
                id=f"t-{action}",
                name="cron",
                arguments={"action": action, "job_id": job["id"]},
            )
            await tool.execute(call)

    actions = [a for a, _ in captured]
    assert actions == ["cron.pause", "cron.resume", "cron.trigger", "cron.remove"]
    for _, fields in captured:
        assert fields["job_id"] == job["id"]
        assert fields["source"] == "cron_tool"


@pytest.mark.asyncio
async def test_list_does_not_emit_audit_log():
    """Read-only actions don't audit."""
    captured: list[tuple[str, dict]] = []
    with patch(
        "opencomputer.dashboard.routes._common.audit_log",
        side_effect=lambda action, **f: captured.append((action, f)),
    ):
        tool = CronTool()
        await tool.execute(
            ToolCall(id="t1", name="cron", arguments={"action": "list"})
        )
    assert not captured, "list should not emit an audit log"


@pytest.mark.asyncio
async def test_audit_failure_does_not_break_action():
    """Audit-log failures must never break cron mutations."""
    with patch(
        "opencomputer.dashboard.routes._common.audit_log",
        side_effect=RuntimeError("audit subsystem down"),
    ):
        tool = CronTool()
        call = ToolCall(
            id="t1",
            name="cron",
            arguments={"action": "create", "schedule": "every 1h", "skill": "x"},
        )
        result = await tool.execute(call)
    assert not result.is_error, result.content
