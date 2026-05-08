"""Hermes parity: notify='origin' delivers back to the chat where the job was created.

The CronTool reads platform/chat_id/thread_id from session_context at create
time and persists them on the job. The scheduler's ``_deliver`` resolves
``notify="origin"`` to those captured values.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.cron.scheduler import _deliver
from opencomputer.gateway.session_context import (
    clear_session_vars,
    set_session_vars,
)
from opencomputer.tools.cron_tool import CronTool
from plugin_sdk.core import ToolCall


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    yield tmp_path


@pytest.mark.asyncio
async def test_crontool_captures_origin_from_session_context():
    set_session_vars(
        platform="telegram",
        chat_id="-100123",
        thread_id="17585",
        user_id="u",
    )
    try:
        tool = CronTool()
        call = ToolCall(
            id="t1",
            name="cron",
            arguments={
                "action": "create",
                "schedule": "every 1h",
                "skill": "x",
                "notify": "origin",
            },
        )
        result = await tool.execute(call)
        assert not result.is_error, result.content
        from opencomputer.cron.jobs import list_jobs
        jobs = list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["origin_platform"] == "telegram"
        assert jobs[0]["origin_chat_id"] == "-100123"
        assert jobs[0]["origin_thread_id"] == "17585"
        assert jobs[0]["notify"] == "origin"
    finally:
        clear_session_vars()


@pytest.mark.asyncio
async def test_deliver_origin_uses_captured_context():
    job = {
        "id": "j1",
        "name": "n",
        "notify": "origin",
        "origin_platform": "slack",
        "origin_chat_id": "#engineering",
    }
    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock(return_value=None)
    with patch.dict(
        "opencomputer.plugins.registry.registry.channels",
        {"slack": fake_adapter},
        clear=False,
    ):
        err = await _deliver(job, "hi")
    assert err is None
    fake_adapter.send.assert_awaited_once_with("#engineering", "hi")


@pytest.mark.asyncio
async def test_deliver_origin_missing_falls_through_to_local():
    job = {"id": "j1", "name": "n", "notify": "origin"}
    err = await _deliver(job, "hi")
    assert err is None  # silent fall-through
