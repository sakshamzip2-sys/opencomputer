"""A3 — RR-7 cron RuntimeContext must set agent_context="cron"
so the consent-bypass guard in MemoryBridge.flush() engages.

The unit tests for the guard mock the input. Production wiring at
``cron/scheduler.py:255`` was leaving ``agent_context`` at default
``"chat"``, so cron-fired turns spun Honcho even though the guard
exists specifically to prevent that.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_cron_run_passes_agent_context_cron_to_runtime() -> None:
    """Production wiring fix: cron RuntimeContext must have agent_context='cron'.

    `_run_one_job(job: dict)` is the cron entry point that builds the
    RuntimeContext and calls AgentLoop.run_conversation.
    """
    from opencomputer.cron import scheduler

    captured: dict[str, object] = {}

    async def fake_run_conversation(*, user_message, runtime):
        captured["runtime"] = runtime
        result = MagicMock()
        result.messages = []
        result.final_response = "ok"
        return result

    fake_loop = MagicMock()
    fake_loop.run_conversation = AsyncMock(side_effect=fake_run_conversation)

    # Stub out the agent-loop builder + the cron-prompt scanner so the
    # test exercises only the RuntimeContext construction path.
    with (
        patch.object(scheduler, "_build_agent_loop", AsyncMock(return_value=fake_loop)),
        patch.object(scheduler, "scan_cron_prompt", lambda _t: None),
    ):
        await scheduler._run_one_job({"id": "test-job", "name": "test", "prompt": "hi"})

    runtime = captured["runtime"]
    assert runtime.agent_context == "cron"
