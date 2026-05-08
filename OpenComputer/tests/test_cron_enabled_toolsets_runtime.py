"""enabled_toolsets must propagate from job dict to AgentLoop.allowed_tools.

Hermes parity: silent-gap fix. The field has been stored on jobs since the
context_from port but never threaded into the AgentLoop, so cron jobs
ignored their toolset allowlist at run time.

Production-grade (2026-05-09): _build_agent_loop now raises
CronAgentLoopBuildError instead of returning a stub when provider can't
resolve. These tests mock _resolve_provider to exercise the real path.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencomputer.cron.scheduler import (
    CronAgentLoopBuildError,
    _build_agent_loop,
)


@pytest.fixture
def mock_provider():
    with patch("opencomputer.cli._resolve_provider", return_value=MagicMock()) as m:
        yield m


@pytest.mark.asyncio
async def test_enabled_toolsets_propagates_to_loop(mock_provider):
    job = {"id": "j1", "name": "n", "enabled_toolsets": ["Read", "Grep"]}
    loop = await _build_agent_loop(job)
    assert loop.allowed_tools is not None
    assert set(loop.allowed_tools) == {"Read", "Grep"}


@pytest.mark.asyncio
async def test_no_toolsets_means_no_allowlist(mock_provider):
    job = {"id": "j1", "name": "n", "enabled_toolsets": None}
    loop = await _build_agent_loop(job)
    # Default behavior — no allowlist set ⇒ inherits parent's full registry.
    assert loop.allowed_tools is None


@pytest.mark.asyncio
async def test_empty_toolsets_list_means_no_tools(mock_provider):
    job = {"id": "j1", "name": "n", "enabled_toolsets": []}
    loop = await _build_agent_loop(job)
    assert loop.allowed_tools == frozenset()


@pytest.mark.asyncio
async def test_provider_resolution_failure_raises():
    """Production-grade: failure surfaces as a typed exception, not a stub."""
    with patch(
        "opencomputer.cli._resolve_provider",
        side_effect=RuntimeError("plugin not loaded"),
    ):
        with pytest.raises(CronAgentLoopBuildError, match="cannot resolve provider"):
            await _build_agent_loop({"id": "j1", "name": "n"})


@pytest.mark.asyncio
async def test_provider_returns_none_raises():
    """A None return from _resolve_provider also raises (production-grade)."""
    with patch("opencomputer.cli._resolve_provider", return_value=None):
        with pytest.raises(CronAgentLoopBuildError, match="resolved to None"):
            await _build_agent_loop({"id": "j1", "name": "n"})
