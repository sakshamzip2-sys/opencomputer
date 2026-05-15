"""MCPManager integration with BundleMcpRegistry (M1 — mcp-openclaw-port).

Validates the splicing path: ``MCPManager.connect_all`` walks
:data:`opencomputer.mcp.bundle.default_registry` and includes bundle
servers in the connect list. Doesn't actually spawn subprocesses —
each fake MCPConnection records the config it was constructed with so
we can assert the merge happened correctly.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.agent.config import MCPServerConfig
from opencomputer.mcp.bundle import default_registry
from opencomputer.mcp.client import MCPManager
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import BundleMcpServer


@pytest.fixture(autouse=True)
def _isolate_default_registry() -> Generator[None, None, None]:
    """Ensure a clean default_registry across these tests."""
    default_registry.clear()
    yield
    default_registry.clear()


class _FakeConnection:
    """Pretend-connection that records its construction config + reports 'connected'."""

    instances: list[_FakeConnection] = []

    def __init__(self, *, config: MCPServerConfig, **_kwargs: object) -> None:
        self.config = config
        self.tools: list = []  # no tools registered; manager just counts
        _FakeConnection.instances.append(self)

    async def connect(self, **_kw: object) -> bool:
        return True  # claim success — tools list is empty so count stays 0

    async def disconnect(self, **_kw: object) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_fake_connection() -> Generator[None, None, None]:
    _FakeConnection.instances.clear()
    yield
    _FakeConnection.instances.clear()


@pytest.mark.asyncio
async def test_connect_all_merges_eager_bundle_servers(tmp_path: Path) -> None:
    """``include_bundle=True`` includes eager (lazy=False) bundle configs."""
    plug_root = tmp_path / "plug-a"
    plug_root.mkdir()
    default_registry.register_plugin_servers(
        "plug-a",
        plug_root,
        (BundleMcpServer(name="memory", command="npx", lazy=False),),
    )

    mgr = MCPManager(tool_registry=ToolRegistry())
    user_cfg = MCPServerConfig(name="my-server", command="echo", enabled=True)

    from opencomputer.mcp import client as client_mod

    with patch.object(client_mod, "MCPConnection", _FakeConnection):
        await mgr.connect_all([user_cfg], include_bundle=True)

    names = {c.config.name for c in _FakeConnection.instances}
    assert "my-server" in names
    assert "plug-a__memory" in names


@pytest.mark.asyncio
async def test_connect_all_lazy_bundle_servers_not_auto_mounted(
    tmp_path: Path,
) -> None:
    """M1 lazy semantics: lazy=True (default) bundle servers stay disabled
    in the derived MCPServerConfig, so connect_all skips them entirely."""
    plug_root = tmp_path / "plug-a"
    plug_root.mkdir()
    default_registry.register_plugin_servers(
        "plug-a",
        plug_root,
        # Default lazy=True
        (BundleMcpServer(name="memory", command="npx"),),
    )

    mgr = MCPManager(tool_registry=ToolRegistry())
    user_cfg = MCPServerConfig(name="my-server", command="echo", enabled=True)

    from opencomputer.mcp import client as client_mod

    with patch.object(client_mod, "MCPConnection", _FakeConnection):
        await mgr.connect_all([user_cfg], include_bundle=True)

    names = {c.config.name for c in _FakeConnection.instances}
    assert "my-server" in names
    # Lazy bundle not auto-spawned.
    assert "plug-a__memory" not in names


@pytest.mark.asyncio
async def test_connect_all_skips_bundle_when_include_bundle_false(
    tmp_path: Path,
) -> None:
    plug_root = tmp_path / "plug-a"
    plug_root.mkdir()
    default_registry.register_plugin_servers(
        "plug-a",
        plug_root,
        (BundleMcpServer(name="memory", command="npx", lazy=False),),
    )
    mgr = MCPManager(tool_registry=ToolRegistry())
    user_cfg = MCPServerConfig(name="my-server", command="echo", enabled=True)

    from opencomputer.mcp import client as client_mod

    with patch.object(client_mod, "MCPConnection", _FakeConnection):
        await mgr.connect_all([user_cfg], include_bundle=False)

    names = {c.config.name for c in _FakeConnection.instances}
    assert "my-server" in names
    assert "plug-a__memory" not in names


@pytest.mark.asyncio
async def test_connect_all_user_config_shadows_bundle_name(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """User-configured server name wins over a bundle entry with the same name."""
    plug_root = tmp_path / "plug-a"
    plug_root.mkdir()
    default_registry.register_plugin_servers(
        "plug-a",
        plug_root,
        # Use lazy=False so the bundle actually shows up in the merge.
        (BundleMcpServer(name="memory", command="npx", lazy=False),),
    )
    mgr = MCPManager(tool_registry=ToolRegistry())
    # Same final name "plug-a__memory" — user wins.
    user_cfg = MCPServerConfig(
        name="plug-a__memory", command="custom", enabled=True,
    )

    from opencomputer.mcp import client as client_mod

    with caplog.at_level(logging.WARNING, logger="opencomputer.mcp.client"):
        with patch.object(client_mod, "MCPConnection", _FakeConnection):
            await mgr.connect_all([user_cfg], include_bundle=True)

    names = [c.config.name for c in _FakeConnection.instances]
    commands = [c.config.command for c in _FakeConnection.instances]
    assert names.count("plug-a__memory") == 1
    assert "custom" in commands
    assert any("shadowed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_connect_all_no_servers_no_bundle_returns_zero() -> None:
    mgr = MCPManager(tool_registry=ToolRegistry())
    n = await mgr.connect_all([], include_bundle=True)
    assert n == 0


@pytest.mark.asyncio
async def test_connect_all_disabled_bundle_servers_not_connected(
    tmp_path: Path,
) -> None:
    """Bundle servers with enabled=False (custom) skip the connect path."""
    plug_root = tmp_path / "plug-a"
    plug_root.mkdir()
    default_registry.register_plugin_servers(
        "plug-a",
        plug_root,
        (BundleMcpServer(name="memory", command="npx"),),
    )
    # Manually disable the registered entry to simulate a user opt-out.
    cfg = default_registry.servers_for_plugin("plug-a")[0]
    from dataclasses import replace as _replace
    default_registry.replace_config(
        "plug-a", "memory", _replace(cfg, enabled=False),
    )

    mgr = MCPManager(tool_registry=ToolRegistry())

    from opencomputer.mcp import client as client_mod

    with patch.object(client_mod, "MCPConnection", _FakeConnection):
        await mgr.connect_all([], include_bundle=True)

    names = {c.config.name for c in _FakeConnection.instances}
    assert "plug-a__memory" not in names
