"""Tests for MCPManager.start_health_loop / stop_health_loop (T1 of mcp-deferrals-v2)."""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.mcp.client import MCPManager


def _fake_registry():
    """Minimal stand-in for the ToolRegistry — None is acceptable since
    health-loop methods don't dispatch tools."""
    return None


@pytest.mark.asyncio
async def test_start_health_loop_returns_a_task(monkeypatch):
    """start_health_loop returns the active asyncio.Task."""
    mgr = MCPManager(tool_registry=_fake_registry())

    # Make sleep return immediately so the loop body runs
    _real_sleep = asyncio.sleep
    async def _no_sleep(secs):
        # Yield to the event loop once so the background task can advance,
        # but don't actually wait `secs` seconds.
        await _real_sleep(0)
    monkeypatch.setattr("opencomputer.mcp.client.asyncio.sleep", _no_sleep)

    # Stub health_check_all so it can't fail / does nothing
    async def _noop_health(self):
        return None
    monkeypatch.setattr(MCPManager, "health_check_all", _noop_health)

    task = mgr.start_health_loop(interval_seconds=30.0)
    assert isinstance(task, asyncio.Task)
    mgr.stop_health_loop()


@pytest.mark.asyncio
async def test_health_loop_invokes_periodically(monkeypatch):
    """The background loop calls health_check_all on each tick."""
    mgr = MCPManager(tool_registry=_fake_registry())

    invocations: list[int] = []

    async def _track(self):
        invocations.append(1)

    _real_sleep = asyncio.sleep
    async def _no_sleep(secs):
        # Yield to the event loop once so the background task can advance,
        # but don't actually wait `secs` seconds.
        await _real_sleep(0)

    monkeypatch.setattr(MCPManager, "health_check_all", _track)
    monkeypatch.setattr("opencomputer.mcp.client.asyncio.sleep", _no_sleep)

    mgr.start_health_loop(interval_seconds=30.0)
    # Yield control so the background task gets to run
    for _ in range(5):
        await asyncio.sleep(0)
    mgr.stop_health_loop()

    # At least a few iterations should have fired
    assert len(invocations) >= 1


@pytest.mark.asyncio
async def test_start_health_loop_idempotent(monkeypatch):
    """Calling start_health_loop twice returns the SAME task (no duplication)."""
    mgr = MCPManager(tool_registry=_fake_registry())

    _real_sleep = asyncio.sleep
    async def _no_sleep(secs):
        # Yield to the event loop once so the background task can advance,
        # but don't actually wait `secs` seconds.
        await _real_sleep(0)
    async def _noop_health(self):
        return None
    monkeypatch.setattr("opencomputer.mcp.client.asyncio.sleep", _no_sleep)
    monkeypatch.setattr(MCPManager, "health_check_all", _noop_health)

    task1 = mgr.start_health_loop()
    task2 = mgr.start_health_loop()
    assert task1 is task2
    mgr.stop_health_loop()


@pytest.mark.asyncio
async def test_stop_health_loop_cancels_task(monkeypatch):
    """After stop_health_loop, the task is cancelled / done."""
    mgr = MCPManager(tool_registry=_fake_registry())

    _real_sleep = asyncio.sleep
    async def _no_sleep(secs):
        # Yield to the event loop once so the background task can advance,
        # but don't actually wait `secs` seconds.
        await _real_sleep(0)
    async def _noop_health(self):
        return None
    monkeypatch.setattr("opencomputer.mcp.client.asyncio.sleep", _no_sleep)
    monkeypatch.setattr(MCPManager, "health_check_all", _noop_health)

    task = mgr.start_health_loop()
    mgr.stop_health_loop()
    # Yield so the cancel takes effect
    for _ in range(3):
        await asyncio.sleep(0)
    assert task.done() or task.cancelled()


@pytest.mark.asyncio
async def test_stop_health_loop_when_not_started_is_noop():
    """Calling stop without start should not raise."""
    mgr = MCPManager(tool_registry=_fake_registry())
    mgr.stop_health_loop()  # no-op


@pytest.mark.asyncio
async def test_health_loop_continues_on_probe_exception(monkeypatch):
    """If health_check_all raises, the loop logs + continues (doesn't crash)."""
    mgr = MCPManager(tool_registry=_fake_registry())

    invocations = {"count": 0}

    async def _raises_then_succeeds(self):
        invocations["count"] += 1
        if invocations["count"] == 1:
            raise RuntimeError("simulated probe failure")

    _real_sleep = asyncio.sleep
    async def _no_sleep(secs):
        # Yield to the event loop once so the background task can advance,
        # but don't actually wait `secs` seconds.
        await _real_sleep(0)

    monkeypatch.setattr(MCPManager, "health_check_all", _raises_then_succeeds)
    monkeypatch.setattr("opencomputer.mcp.client.asyncio.sleep", _no_sleep)

    mgr.start_health_loop()
    for _ in range(8):
        await asyncio.sleep(0)
    mgr.stop_health_loop()

    # Loop should have continued past the raise — at least 2 invocations
    assert invocations["count"] >= 2
