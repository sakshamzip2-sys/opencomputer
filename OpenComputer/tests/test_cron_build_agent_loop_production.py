"""Integration tests for _build_agent_loop's PRODUCTION path.

The implementation has a stub fallback for tests with no loaded
registry. These tests force the real path by mocking _resolve_provider
to return a fake-but-valid provider, ensuring enabled_toolsets propagates
through the actual AgentLoop, not just the stub.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencomputer.cron.scheduler import _build_agent_loop


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    yield tmp_path


@pytest.mark.asyncio
async def test_real_path_propagates_enabled_toolsets():
    fake_provider = MagicMock()
    with patch("opencomputer.cli._resolve_provider", return_value=fake_provider):
        job = {"id": "j1", "name": "n", "enabled_toolsets": ["Read", "Grep"]}
        loop = await _build_agent_loop(job)

    # When the real path runs, the loop is an AgentLoop instance — not the stub.
    from opencomputer.agent.loop import AgentLoop
    assert isinstance(loop, AgentLoop), (
        f"expected AgentLoop in production path, got {type(loop).__name__}"
    )
    assert loop.allowed_tools == frozenset({"Read", "Grep"})
    assert loop.provider is fake_provider


@pytest.mark.asyncio
async def test_real_path_no_toolsets_keeps_default():
    fake_provider = MagicMock()
    with patch("opencomputer.cli._resolve_provider", return_value=fake_provider):
        job = {"id": "j1", "name": "n", "enabled_toolsets": None}
        loop = await _build_agent_loop(job)

    from opencomputer.agent.loop import AgentLoop
    assert isinstance(loop, AgentLoop)
    # AgentLoop.__init__ sets allowed_tools=None by default.
    assert loop.allowed_tools is None


@pytest.mark.asyncio
async def test_real_path_iteration_cap_applied():
    """Cron loops should cap max_iterations at 30."""
    fake_provider = MagicMock()
    with patch("opencomputer.cli._resolve_provider", return_value=fake_provider):
        job = {"id": "j1", "name": "n"}
        loop = await _build_agent_loop(job)

    from opencomputer.agent.loop import AgentLoop
    assert isinstance(loop, AgentLoop)
    assert loop.config.loop.max_iterations <= 30
