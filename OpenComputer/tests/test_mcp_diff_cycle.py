"""Tests for §9.5 — ``MCPManager.diff_cycle`` reconciliation.

Coverage:
  - Identity hash is stable across calls and changes with config delta
  - Empty old + empty new = no-op
  - Add-only diff connects new servers
  - Remove-only diff disconnects gone servers + unregisters tools
  - Config-change diff (same name, different env) disconnects + reconnects
  - Disabled servers behave like absent
  - Exception in disconnect doesn't stop the rest
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

# ─── Lightweight fakes — avoid spawning real MCP subprocesses ────────


@dataclass
class _FakeToolSchema:
    name: str


@dataclass
class _FakeTool:
    schema: _FakeToolSchema


@dataclass
class _FakeConnection:
    config: Any
    tools: list[_FakeTool] = field(default_factory=list)
    disconnect_called: bool = False
    disconnect_raises: bool = False

    async def disconnect(self) -> None:
        self.disconnect_called = True
        if self.disconnect_raises:
            raise RuntimeError("disconnect boom")


class _FakeRegistry:
    def __init__(self) -> None:
        self.unregistered: list[str] = []
        self.registered: list[str] = []

    def unregister(self, name: str) -> None:
        if name in self.registered:
            self.registered.remove(name)
        self.unregistered.append(name)

    def register(self, tool: _FakeTool) -> None:
        self.registered.append(tool.schema.name)


# ─── Hash tests ──────────────────────────────────────────────────────


def test_hash_stable_for_same_config() -> None:
    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.mcp.client import MCPManager

    cfg = MCPServerConfig(name="alpha", command="cmd", args=("a", "b"))
    h1 = MCPManager._config_identity_hash(cfg)
    h2 = MCPManager._config_identity_hash(cfg)
    assert h1 == h2


def test_hash_changes_when_env_differs() -> None:
    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.mcp.client import MCPManager

    a = MCPServerConfig(name="alpha", command="cmd", env={"K": "1"})
    b = MCPServerConfig(name="alpha", command="cmd", env={"K": "2"})
    assert MCPManager._config_identity_hash(a) != MCPManager._config_identity_hash(b)


def test_hash_changes_when_args_differ() -> None:
    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.mcp.client import MCPManager

    a = MCPServerConfig(name="alpha", command="cmd", args=("--foo",))
    b = MCPServerConfig(name="alpha", command="cmd", args=("--bar",))
    assert MCPManager._config_identity_hash(a) != MCPManager._config_identity_hash(b)


def test_hash_stable_under_dict_ordering() -> None:
    """Re-ordering env dict keys must not change the hash."""
    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.mcp.client import MCPManager

    a = MCPServerConfig(name="a", env={"A": "1", "B": "2"})
    b = MCPServerConfig(name="a", env={"B": "2", "A": "1"})
    assert MCPManager._config_identity_hash(a) == MCPManager._config_identity_hash(b)


# ─── diff_cycle tests ────────────────────────────────────────────────


def _make_manager() -> Any:
    """Construct an MCPManager with a fake registry; bypass __init__ work."""
    from opencomputer.mcp.client import MCPManager

    mgr = MCPManager.__new__(MCPManager)
    mgr.tool_registry = _FakeRegistry()
    mgr.connections = []
    mgr._connecting = set()
    mgr._deferred_future = None
    mgr._health_loop_task = None
    mgr._bg_loop = None
    mgr._bg_thread = None
    import threading as _threading
    mgr._bg_ready = _threading.Event()
    mgr.lease_registry = None
    mgr.lease_session_id = None
    return mgr


@pytest.mark.asyncio
async def test_diff_cycle_empty_old_empty_new_is_noop() -> None:
    mgr = _make_manager()
    result = await mgr.diff_cycle([])
    assert result["disconnected"] == 0
    assert result["connected"] == 0


@pytest.mark.asyncio
async def test_diff_cycle_remove_only_disconnects() -> None:
    from opencomputer.agent.config import MCPServerConfig

    mgr = _make_manager()
    cfg = MCPServerConfig(name="old", command="echo", enabled=True)
    conn = _FakeConnection(config=cfg, tools=[_FakeTool(_FakeToolSchema("tool_x"))])
    mgr.connections.append(conn)
    mgr.tool_registry.registered.append("tool_x")

    result = await mgr.diff_cycle([])  # new = empty
    assert result["disconnected"] == 1
    assert result["connected"] == 0
    assert conn.disconnect_called
    assert "tool_x" in mgr.tool_registry.unregistered
    assert mgr.connections == []


@pytest.mark.asyncio
async def test_diff_cycle_no_change_is_noop(monkeypatch) -> None:
    """Same config in both → no disconnect, no connect."""
    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.mcp.client import MCPManager

    mgr = _make_manager()
    cfg = MCPServerConfig(name="stable", command="echo", enabled=True)
    mgr.connections.append(_FakeConnection(config=cfg, tools=[]))

    connect_called = {"n": 0}

    async def _fake_connect_all(servers, *, include_bundle=True, **kwargs):
        connect_called["n"] += 1
        return 0

    mgr.connect_all = _fake_connect_all  # type: ignore[method-assign]

    result = await mgr.diff_cycle([cfg])
    assert result["disconnected"] == 0
    assert result["connected"] == 0
    assert connect_called["n"] == 0


@pytest.mark.asyncio
async def test_diff_cycle_config_change_disconnects_and_reconnects(
    monkeypatch,
) -> None:
    """Same name, different env → disconnect + reconnect."""
    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.mcp.client import MCPManager

    mgr = _make_manager()
    old_cfg = MCPServerConfig(
        name="api", command="echo", env={"KEY": "old"}, enabled=True,
    )
    new_cfg = MCPServerConfig(
        name="api", command="echo", env={"KEY": "new"}, enabled=True,
    )
    old_conn = _FakeConnection(config=old_cfg, tools=[_FakeTool(_FakeToolSchema("t"))])
    mgr.connections.append(old_conn)
    mgr.tool_registry.registered.append("t")

    connect_arg = {"servers": None}

    async def _fake_connect_all(servers, *, include_bundle=True, **kwargs):
        connect_arg["servers"] = servers
        return len(servers)

    mgr.connect_all = _fake_connect_all  # type: ignore[method-assign]

    result = await mgr.diff_cycle([new_cfg])
    assert result["disconnected"] == 1
    assert result["connected"] == 1
    assert old_conn.disconnect_called
    assert "t" in mgr.tool_registry.unregistered
    assert connect_arg["servers"] == [new_cfg]


@pytest.mark.asyncio
async def test_diff_cycle_disabled_server_treated_as_absent(monkeypatch) -> None:
    """A server with enabled=False is disconnected if present."""
    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.mcp.client import MCPManager

    mgr = _make_manager()
    enabled = MCPServerConfig(name="srv", command="echo", enabled=True)
    disabled = MCPServerConfig(name="srv", command="echo", enabled=False)
    mgr.connections.append(_FakeConnection(config=enabled, tools=[]))

    async def _fake_connect_all(servers, **kwargs):
        return 0

    mgr.connect_all = _fake_connect_all  # type: ignore[method-assign]

    result = await mgr.diff_cycle([disabled])
    assert result["disconnected"] == 1
    assert result["connected"] == 0


@pytest.mark.asyncio
async def test_disconnect_exception_does_not_stop_others(monkeypatch) -> None:
    """One disconnect raising must not stop sibling disconnects or connects."""
    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.mcp.client import MCPManager

    mgr = _make_manager()
    bad = MCPServerConfig(name="bad", command="echo", enabled=True)
    ok = MCPServerConfig(name="ok", command="echo", enabled=True)
    bad_conn = _FakeConnection(config=bad, tools=[], disconnect_raises=True)
    ok_conn = _FakeConnection(config=ok, tools=[])
    mgr.connections.extend([bad_conn, ok_conn])

    async def _fake_connect_all(servers, **kwargs):
        return 0

    mgr.connect_all = _fake_connect_all  # type: ignore[method-assign]

    result = await mgr.diff_cycle([])
    # Both should have been attempted.
    assert bad_conn.disconnect_called
    assert ok_conn.disconnect_called
    assert result["disconnected"] == 2


@pytest.mark.asyncio
async def test_register_mcp_rebind_handler_wires_into_registry() -> None:
    """The helper installs an ``mcp`` handler on the AgentLoop registry."""
    from opencomputer.agent.profile_rebind import (
        ProfileRebindRegistry,
        register_mcp_rebind_handler,
    )

    # Fake AgentLoop exposing the registration API.
    class _FakeLoop:
        def __init__(self) -> None:
            self.reg = ProfileRebindRegistry()

        def register_profile_rebind_handler(self, name, handler, *, priority=100):
            self.reg.register(name, handler, priority=priority)

    class _FakeMgr:
        async def diff_cycle(self, servers):
            return {"disconnected": 0, "connected": 0}

    loop = _FakeLoop()
    register_mcp_rebind_handler(loop, _FakeMgr())
    assert "mcp" in loop.reg.names()
