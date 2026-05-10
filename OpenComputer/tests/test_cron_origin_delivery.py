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


@pytest.mark.asyncio
async def test_deliver_origin_with_thread_id_telegram_topic():
    """Production-grade: Telegram forum-topic threads are delivered correctly."""
    job = {
        "id": "j1",
        "name": "n",
        "notify": "origin",
        "origin_platform": "telegram",
        "origin_chat_id": "-100123",
        "origin_thread_id": "17585",
    }

    # Thread-aware adapter accepts thread_id kwarg.
    class FakeThreadAdapter:
        def __init__(self):
            self.calls = []

        async def send(self, chat_id, content, *, thread_id=None):
            self.calls.append((chat_id, content, thread_id))
            return None

    adapter = FakeThreadAdapter()
    with patch.dict(
        "opencomputer.plugins.registry.registry.channels",
        {"telegram": adapter},
        clear=False,
    ):
        err = await _deliver(job, "hi topic")
    assert err is None
    assert adapter.calls == [("-100123", "hi topic", "17585")]


@pytest.mark.asyncio
async def test_deliver_explicit_target_with_thread_id():
    """notify='telegram:-100123:17585' is parsed and forwarded."""
    job = {"id": "j1", "name": "n", "notify": "telegram:-100123:17585"}

    captured = {}

    class FakeAdapter:
        async def send(self, chat_id, content, *, thread_id=None):
            captured["chat_id"] = chat_id
            captured["thread_id"] = thread_id
            return None

    with patch.dict(
        "opencomputer.plugins.registry.registry.channels",
        {"telegram": FakeAdapter()},
        clear=False,
    ):
        err = await _deliver(job, "x")
    assert err is None
    assert captured == {"chat_id": "-100123", "thread_id": "17585"}


@pytest.mark.asyncio
async def test_deliver_thread_id_skipped_when_adapter_lacks_kwarg():
    """Production-grade: adapter without thread_id support gets clean call."""
    job = {"id": "j1", "name": "n", "notify": "slack:#x:thread1"}

    class TwoArgAdapter:
        def __init__(self):
            self.calls = []

        async def send(self, chat_id, content):
            self.calls.append((chat_id, content))
            return None

    adapter = TwoArgAdapter()
    with patch.dict(
        "opencomputer.plugins.registry.registry.channels",
        {"slack": adapter},
        clear=False,
    ):
        err = await _deliver(job, "msg")
    assert err is None
    # Adapter called with two positional args, no thread_id.
    assert adapter.calls == [("#x", "msg")]
