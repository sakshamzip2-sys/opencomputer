"""enabled_toolsets must propagate from job dict to AgentLoop.allowed_tools.

Hermes parity: silent-gap fix. The field has been stored on jobs since the
context_from port but never threaded into the AgentLoop, so cron jobs
ignored their toolset allowlist at run time.
"""
from __future__ import annotations

import pytest

from opencomputer.cron.scheduler import _build_agent_loop


@pytest.mark.asyncio
async def test_enabled_toolsets_propagates_to_loop():
    job = {"id": "j1", "name": "n", "enabled_toolsets": ["Read", "Grep"]}
    loop = await _build_agent_loop(job)
    assert loop.allowed_tools is not None
    assert set(loop.allowed_tools) == {"Read", "Grep"}


@pytest.mark.asyncio
async def test_no_toolsets_means_no_allowlist():
    job = {"id": "j1", "name": "n", "enabled_toolsets": None}
    loop = await _build_agent_loop(job)
    # Default behavior — no allowlist set ⇒ inherits parent's full registry.
    assert loop.allowed_tools is None


@pytest.mark.asyncio
async def test_empty_toolsets_list_means_no_tools():
    job = {"id": "j1", "name": "n", "enabled_toolsets": []}
    loop = await _build_agent_loop(job)
    assert loop.allowed_tools == frozenset()
