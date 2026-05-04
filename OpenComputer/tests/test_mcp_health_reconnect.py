"""Tests for MCP health-probe + auto-reconnect (T3 of tier-2 trio)."""

from __future__ import annotations

import pytest

from opencomputer.mcp.client import MCPConnection, MCPManager


class _FakeConfig:
    """Minimal stand-in for MCPServerConfig — only the fields the methods touch."""

    def __init__(self, name: str = "fake"):
        self.name = name
        self.enabled = True
        self.transport = "stdio"
        self.command = "/bin/true"
        self.args = ()
        self.env = {}


def test_mcpconnection_has_health_fields():
    """MCPConnection tracks last_health_check_at + reconnect counters."""
    conn = MCPConnection(config=_FakeConfig())
    assert conn.last_health_check_at is None
    assert conn.reconnect_attempts == 0
    assert conn.reconnect_window_start is None


@pytest.mark.asyncio
async def test_attempt_reconnect_increments_counter(monkeypatch):
    """attempt_reconnect counts attempts and respects backoff (mocked sleep)."""
    conn = MCPConnection(config=_FakeConfig())
    conn.state = "error"

    # Mock asyncio.sleep at the module path so the test doesn't actually wait.
    async def _no_sleep(secs):
        return None
    monkeypatch.setattr("opencomputer.mcp.client.asyncio.sleep", _no_sleep)

    # Class-level method patch so bound-method dispatch picks up the stub.
    async def _fake_connect(self, **kwargs):
        return False
    async def _noop_disconnect(self, *args, **kwargs):
        return None
    monkeypatch.setattr(MCPConnection, "connect", _fake_connect)
    monkeypatch.setattr(MCPConnection, "disconnect", _noop_disconnect)

    result = await conn.attempt_reconnect()
    assert result is False  # _fake_connect returned False
    assert conn.reconnect_attempts == 1


@pytest.mark.asyncio
async def test_attempt_reconnect_caps_at_three_per_minute(monkeypatch):
    """Fourth attempt within 60s is refused with rate-limit warning."""
    conn = MCPConnection(config=_FakeConfig())
    conn.state = "error"

    async def _no_sleep(secs):
        return None
    monkeypatch.setattr("opencomputer.mcp.client.asyncio.sleep", _no_sleep)

    async def _fake_connect(self, **kwargs):
        return False
    async def _noop_disconnect(self, *args, **kwargs):
        return None
    monkeypatch.setattr(MCPConnection, "connect", _fake_connect)
    monkeypatch.setattr(MCPConnection, "disconnect", _noop_disconnect)

    for _ in range(3):
        await conn.attempt_reconnect()
    assert conn.reconnect_attempts == 3

    # Fourth refuses (rate-limited)
    result = await conn.attempt_reconnect()
    assert result is False
    assert conn.reconnect_attempts == 3  # didn't increment


@pytest.mark.asyncio
async def test_health_check_marks_unhealthy_on_probe_failure(monkeypatch):
    """When the probe raises, connection state flips to 'error'."""
    conn = MCPConnection(config=_FakeConfig())
    conn.state = "connected"

    async def _failing_probe(self):
        raise RuntimeError("probe failed")
    monkeypatch.setattr(MCPConnection, "_probe_alive", _failing_probe)

    result = await conn.health_check()
    assert result is False
    assert conn.state == "error"
    assert conn.last_health_check_at is not None
    assert conn.last_error == "probe failed"


@pytest.mark.asyncio
async def test_health_check_returns_true_when_alive(monkeypatch):
    conn = MCPConnection(config=_FakeConfig())
    conn.state = "connected"

    async def _ok_probe(self):
        return None
    monkeypatch.setattr(MCPConnection, "_probe_alive", _ok_probe)

    result = await conn.health_check()
    assert result is True
    assert conn.state == "connected"  # unchanged
    assert conn.last_health_check_at is not None


@pytest.mark.asyncio
async def test_health_check_skips_when_not_connected():
    """A 'disconnected' or 'error' connection skips the probe but still
    records the timestamp."""
    conn = MCPConnection(config=_FakeConfig())
    conn.state = "disconnected"
    result = await conn.health_check()
    assert result is False
    assert conn.last_health_check_at is not None


@pytest.mark.asyncio
async def test_manager_health_check_all_iterates_only_connected(monkeypatch):
    """MCPManager.health_check_all probes connected servers, skips others."""
    mgr = MCPManager(tool_registry=None)
    conn1 = MCPConnection(config=_FakeConfig("a"))
    conn1.state = "connected"
    conn2 = MCPConnection(config=_FakeConfig("b"))
    conn2.state = "disconnected"  # should be skipped
    conn3 = MCPConnection(config=_FakeConfig("c"))
    conn3.state = "connected"
    mgr.connections = [conn1, conn2, conn3]

    probed: list[str] = []

    async def _track_probe(self):
        probed.append(self.config.name)
    monkeypatch.setattr(MCPConnection, "_probe_alive", _track_probe)

    await mgr.health_check_all()
    assert probed == ["a", "c"]


@pytest.mark.asyncio
async def test_reconnect_window_resets_after_60s(monkeypatch):
    """After 60s elapse, the per-window attempt counter resets."""
    conn = MCPConnection(config=_FakeConfig())
    conn.state = "error"

    async def _no_sleep(secs):
        return None
    monkeypatch.setattr("opencomputer.mcp.client.asyncio.sleep", _no_sleep)

    async def _fake_connect(self, **kwargs):
        return False
    async def _noop_disconnect(self, *args, **kwargs):
        return None
    monkeypatch.setattr(MCPConnection, "connect", _fake_connect)
    monkeypatch.setattr(MCPConnection, "disconnect", _noop_disconnect)

    # Burn 3 attempts
    for _ in range(3):
        await conn.attempt_reconnect()

    # Simulate 60+s elapsed by rewinding window_start
    conn.reconnect_window_start -= 61.0

    # Should reset and allow another attempt
    await conn.attempt_reconnect()
    assert conn.reconnect_attempts == 1  # reset to 1 on this attempt
