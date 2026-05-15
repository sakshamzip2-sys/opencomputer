"""Audit-followup integration tests (post-brutal-honesty pass).

Covers the wiring gaps identified during the second brutal audit:

* MCPTool ``_display_name_override`` has a class-level default so
  test doubles via ``__new__`` don't AttributeError on schema access.
* MCPManager construction triggers a startup orphan sweep (Gap A).
* MCPManager._connect_one assigns lease bindings when the manager is
  session-bound (Gap F wiring through to MCPTool).
* MCPTool.execute acquires + releases a lease around dispatch (Gap F
  wiring through to session-scoped runtime).
* Gap D's compose pipeline actually fires during connect_all
  (integration with the helper).
* Gap E redaction is wrapped around the connect-raise warning.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.mcp.client import MCPTool

# ─── MCPTool class-level defaults survive __new__ ───────────────


def test_mcptool_new_without_init_does_not_error_on_schema() -> None:
    """Test doubles built via ``MCPTool.__new__(MCPTool)`` set only the
    fields they need + access ``schema`` — class-level defaults must
    cover the missing ones."""
    tool = MCPTool.__new__(MCPTool)
    tool.server_name = "srv"
    tool.tool_name = "echo"
    tool.description = ""
    tool.parameters = {}
    # Note: _display_name_override / timeout / session_loop / _lease_*
    # are NOT set. Class-level defaults must apply.
    schema = tool.schema
    assert schema.name == "srv__echo"


# ─── Gap A — MCPManager startup orphan sweep ─────────────────────


def test_mcpmanager_construction_calls_orphan_sweep() -> None:
    from opencomputer.mcp.client import MCPManager
    from opencomputer.tools.registry import ToolRegistry

    with patch(
        "opencomputer.mcp.process_tree.kill_mcp_descendants",
        return_value=(0, 0),
    ) as patched:
        MCPManager(tool_registry=ToolRegistry())
        patched.assert_called_once()


def test_mcpmanager_construction_swallows_sweep_errors() -> None:
    from opencomputer.mcp.client import MCPManager
    from opencomputer.tools.registry import ToolRegistry

    with patch(
        "opencomputer.mcp.process_tree.kill_mcp_descendants",
        side_effect=RuntimeError("psutil broken"),
    ):
        # Must not raise — sweep is best-effort
        MCPManager(tool_registry=ToolRegistry())


# ─── Gap F — lease bindings on MCPTool via MCPManager ───────────


def test_mcptool_execute_acquires_lease_when_bound() -> None:
    from opencomputer.mcp.lease import LeaseRegistry
    from plugin_sdk.core import ToolCall

    leases = LeaseRegistry()
    fake_result = MagicMock()
    fake_result.content = [MagicMock(text="ok", type="text")]
    fake_result.isError = False
    session = MagicMock()

    async def _call_tool(name=None, arguments=None):
        # Inside the call, the lease should be held.
        assert leases.has_active_lease("session-a")
        return fake_result

    session.call_tool = _call_tool
    tool = MCPTool.__new__(MCPTool)
    tool.server_name = "srv"
    tool.tool_name = "x"
    tool.description = ""
    tool.parameters = {}
    tool.session = session
    tool.timeout = 30.0
    tool.session_loop = None
    tool._lease_registry = leases
    tool._lease_session_id = "session-a"

    asyncio.run(tool.execute(ToolCall(id="c1", name="x", arguments={})))
    # After dispatch, the lease was released
    assert not leases.has_active_lease("session-a")


def test_mcptool_execute_releases_lease_on_exception() -> None:
    from opencomputer.mcp.lease import LeaseRegistry
    from plugin_sdk.core import ToolCall

    leases = LeaseRegistry()

    async def _call_tool(name=None, arguments=None):
        raise RuntimeError("transport down")

    session = MagicMock()
    session.call_tool = _call_tool
    tool = MCPTool.__new__(MCPTool)
    tool.server_name = "srv"
    tool.tool_name = "x"
    tool.description = ""
    tool.parameters = {}
    tool.session = session
    tool.timeout = 30.0
    tool.session_loop = None
    tool._lease_registry = leases
    tool._lease_session_id = "session-a"

    result = asyncio.run(tool.execute(ToolCall(id="c1", name="x", arguments={})))
    assert result.is_error
    # Lease released even on exception path
    assert not leases.has_active_lease("session-a")


def test_mcptool_execute_no_lease_when_unbound() -> None:
    """The default path (no lease registry) still dispatches cleanly."""
    from plugin_sdk.core import ToolCall

    fake_result = MagicMock()
    fake_result.content = [MagicMock(text="ok", type="text")]
    fake_result.isError = False
    session = MagicMock()

    async def _call_tool(name=None, arguments=None):
        return fake_result

    session.call_tool = _call_tool
    tool = MCPTool.__new__(MCPTool)
    tool.server_name = "srv"
    tool.tool_name = "x"
    tool.description = ""
    tool.parameters = {}
    tool.session = session
    tool.timeout = 30.0
    tool.session_loop = None
    # _lease_* not set — class-level defaults (None) take over.
    result = asyncio.run(tool.execute(ToolCall(id="c1", name="x", arguments={})))
    assert not result.is_error


def test_session_runtime_binds_lease_on_manager() -> None:
    """SessionMcpRuntimeManager.get_or_create sets the binding."""
    from opencomputer.mcp.lease import LeaseRegistry
    from opencomputer.mcp.session_runtime import SessionMcpRuntimeManager

    instances: list[MagicMock] = []

    def factory() -> MagicMock:
        m = MagicMock(stop_background_loop=MagicMock(), connections=[])
        instances.append(m)
        return m

    leases = LeaseRegistry()
    mgr = SessionMcpRuntimeManager(
        mcp_manager_factory=factory, lease_registry=leases,
    )
    m1 = mgr.get_or_create("session-a")
    assert m1.lease_registry is leases
    assert m1.lease_session_id == "session-a"


# ─── Gap E — redaction wraps MCP error logger calls ─────────────


def test_connect_raise_log_redacts_url_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When a connect raises with a URL+token in the message, the
    logged warning has the token redacted."""
    import logging

    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.mcp.client import MCPManager
    from opencomputer.tools.registry import ToolRegistry

    secret_url_msg = (
        "ConnectionError: GET https://api.example.com/?token=ohnoSecret123 failed"
    )

    class _FakeConn:
        def __init__(self, *, config, **_kw):
            self.config = config
            self.tools = []

        async def connect(self, **_kw):
            raise RuntimeError(secret_url_msg)

    mgr = MCPManager(tool_registry=ToolRegistry())
    cfg = MCPServerConfig(name="test", command="echo", enabled=True)

    from opencomputer.mcp import client as client_mod

    with patch.object(client_mod, "MCPConnection", _FakeConn):
        with caplog.at_level(logging.WARNING, logger="opencomputer.mcp.client"):
            asyncio.run(mgr.connect_all([cfg], include_bundle=False))

    msgs = [r.getMessage() for r in caplog.records]
    joined = " ".join(msgs)
    # The token VALUE must be stripped; the structural URL still visible
    assert "ohnoSecret123" not in joined
    assert "api.example.com" in joined or "test" in joined


# ─── Gap D — collision suffix actually fires in connect_all ─────


def test_compose_collision_resolves_at_connect_one() -> None:
    """When two MCP tools share the same composed name, the second
    gets a -2 suffix via the compose pipeline (not silently skipped)."""
    from opencomputer.agent.config import MCPServerConfig
    from opencomputer.mcp.client import MCPManager
    from opencomputer.tools.registry import ToolRegistry

    class _FakeConn:
        instances: list = []

        def __init__(self, *, config, **_kw):
            self.config = config
            _FakeConn.instances.append(self)
            # Each "connected" connection exposes 1 tool named "echo"
            tool = MCPTool.__new__(MCPTool)
            tool.server_name = config.name  # "alpha" then "beta"
            tool.tool_name = "echo"
            tool.description = ""
            tool.parameters = {}
            tool.session = MagicMock()
            tool.timeout = 30.0
            tool.session_loop = None
            self.tools = [tool]

        async def connect(self, **_kw):
            return True

        async def disconnect(self, **_kw):
            pass

    _FakeConn.instances.clear()
    # Two servers with SAME ``name``: the connect path uses the cfg.name as
    # the namespace prefix. Collision happens at the TOOL namespace level
    # — two tools both named "shared__echo" want to register.
    cfg1 = MCPServerConfig(name="shared", command="echo", enabled=True)
    cfg2 = MCPServerConfig(name="shared", command="cat", enabled=True)
    registry = ToolRegistry()
    mgr = MCPManager(tool_registry=registry)

    from opencomputer.mcp import client as client_mod

    with patch.object(client_mod, "MCPConnection", _FakeConn):
        asyncio.run(mgr.connect_all([cfg1, cfg2], include_bundle=False))

    names = sorted(registry.names())
    # Compose should have produced shared__echo AND shared__echo-2
    assert "shared__echo" in names
    assert any(n.startswith("shared__echo-") for n in names), (
        f"expected a collision suffix, got: {names}"
    )


# ─── Gap G — loader-stub end-to-end via PluginCandidate ─────────


@pytest.fixture
def isolate_default_registry() -> Generator[None, None, None]:
    from opencomputer.mcp.bundle import default_registry
    default_registry.clear()
    yield
    default_registry.clear()


def test_loader_registers_lazy_bundle_stubs_on_active_api(
    tmp_path, isolate_default_registry,
) -> None:
    """End-to-end: a candidate with lazy=True + tools declares 2 stubs;
    loader registers them on the api's tool registry."""
    from opencomputer.plugins.discovery import PluginCandidate
    from opencomputer.plugins.loader import _register_bundle_mcps
    from opencomputer.tools.registry import ToolRegistry
    from plugin_sdk.core import (
        BundleMcpServer,
        BundleMcpToolDecl,
        PluginManifest,
    )

    plug_dir = tmp_path / "plug-a"
    plug_dir.mkdir()
    manifest = PluginManifest(
        id="plug-a",
        name="Plug A",
        version="1.0.0",
        entry="plugin",
        bundle_mcp=(
            BundleMcpServer(
                name="memory",
                command="npx",
                lazy=True,
                tools=(
                    BundleMcpToolDecl(name="store", description="Store"),
                    BundleMcpToolDecl(name="recall", description="Recall"),
                ),
            ),
        ),
    )
    cand = PluginCandidate(
        manifest=manifest,
        root_dir=plug_dir,
        manifest_path=plug_dir / "plugin.json",
    )

    # A minimal api stub with a real ToolRegistry
    registry = ToolRegistry()
    api = MagicMock()
    api.tools = registry

    _register_bundle_mcps(cand, api=api)

    names = sorted(registry.names())
    # Composed names: <plugin>__<server>__<tool>
    assert "plug-a__memory__store" in names
    assert "plug-a__memory__recall" in names


def test_loader_no_stubs_when_lazy_false(
    tmp_path, isolate_default_registry,
) -> None:
    """lazy=False bundles don't get stubs (real MCPTool registration
    happens at MCPManager.connect_all time instead)."""
    from opencomputer.plugins.discovery import PluginCandidate
    from opencomputer.plugins.loader import _register_bundle_mcps
    from opencomputer.tools.registry import ToolRegistry
    from plugin_sdk.core import (
        BundleMcpServer,
        BundleMcpToolDecl,
        PluginManifest,
    )

    plug_dir = tmp_path / "plug-b"
    plug_dir.mkdir()
    manifest = PluginManifest(
        id="plug-b",
        name="Plug B",
        version="1.0.0",
        entry="plugin",
        bundle_mcp=(
            BundleMcpServer(
                name="memory",
                command="npx",
                lazy=False,
                tools=(BundleMcpToolDecl(name="store"),),
            ),
        ),
    )
    cand = PluginCandidate(
        manifest=manifest,
        root_dir=plug_dir,
        manifest_path=plug_dir / "plugin.json",
    )
    registry = ToolRegistry()
    api = MagicMock()
    api.tools = registry

    _register_bundle_mcps(cand, api=api)

    # lazy=False → no stubs registered
    assert "plug-b__memory__store" not in registry.names()


def test_loader_no_stubs_when_no_tools_declared(
    tmp_path, isolate_default_registry,
) -> None:
    """lazy=True but no tools declared → no stubs (back-compat path)."""
    from opencomputer.plugins.discovery import PluginCandidate
    from opencomputer.plugins.loader import _register_bundle_mcps
    from opencomputer.tools.registry import ToolRegistry
    from plugin_sdk.core import BundleMcpServer, PluginManifest

    plug_dir = tmp_path / "plug-c"
    plug_dir.mkdir()
    manifest = PluginManifest(
        id="plug-c",
        name="Plug C",
        version="1.0.0",
        entry="plugin",
        bundle_mcp=(BundleMcpServer(name="memory", command="npx", lazy=True),),
    )
    cand = PluginCandidate(
        manifest=manifest,
        root_dir=plug_dir,
        manifest_path=plug_dir / "plugin.json",
    )
    registry = ToolRegistry()
    api = MagicMock()
    api.tools = registry

    _register_bundle_mcps(cand, api=api)
    assert len(list(registry.names())) == 0
