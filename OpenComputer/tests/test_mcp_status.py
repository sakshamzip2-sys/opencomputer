"""IV.4 — Rich MCP server status snapshot + CLI command.

Covers:
- ``MCPManager.status_snapshot()`` returning per-server dict with all
  required fields (name, url, version, tool_count, tools,
  connection_state, last_error, uptime_sec).
- Empty manager → empty list.
- Mocked connected server → snapshot has expected fields + state="connected".
- Mocked disconnected/failed server → state != "connected" with last_error
  populated.
- ``opencomputer mcp status`` CLI renders a Rich table with headers.

Mirrors Kimi CLI's ``mcp_status_snapshot`` pattern at
``sources/kimi-cli/src/kimi_cli/soul/toolset.py`` line 277.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from opencomputer.agent.config import Config, MCPConfig, MCPServerConfig


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _home() so tests don't touch the real ~/.opencomputer."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


# ─── status_snapshot() shape ────────────────────────────────────────


def test_status_snapshot_empty_manager_returns_empty_list() -> None:
    from opencomputer.mcp.client import MCPManager
    from opencomputer.tools.registry import ToolRegistry

    mgr = MCPManager(tool_registry=ToolRegistry())
    snap = mgr.status_snapshot()
    assert snap == []


def test_status_snapshot_connected_server_has_all_fields() -> None:
    from opencomputer.mcp.client import MCPConnection, MCPManager, MCPTool
    from opencomputer.tools.registry import ToolRegistry

    mgr = MCPManager(tool_registry=ToolRegistry())
    cfg = MCPServerConfig(
        name="test-server",
        transport="http",
        url="https://example.com/mcp",
    )
    conn = MCPConnection(config=cfg)
    conn.session = MagicMock()  # fake live session
    conn.version = "1.2.3"
    conn.connect_time = time.monotonic() - 1.5  # 1.5s ago
    conn.state = "connected"
    conn.last_error = None
    # Populate tools (mock MCPTool list)
    conn.tools = [
        MCPTool(
            server_name="test-server",
            tool_name="alpha",
            description="",
            parameters={},
            session=conn.session,
        ),
        MCPTool(
            server_name="test-server",
            tool_name="beta",
            description="",
            parameters={},
            session=conn.session,
        ),
    ]
    mgr.connections.append(conn)

    snap = mgr.status_snapshot()
    assert len(snap) == 1
    entry = snap[0]
    assert set(entry.keys()) == {
        "name",
        "url",
        "version",
        "tool_count",
        "tools",
        "connection_state",
        "last_error",
        "uptime_sec",
    }
    assert entry["name"] == "test-server"
    assert entry["url"] == "https://example.com/mcp"
    assert entry["version"] == "1.2.3"
    assert entry["tool_count"] == 2
    assert entry["tools"] == ["alpha", "beta"]
    assert entry["connection_state"] == "connected"
    assert entry["last_error"] is None
    assert isinstance(entry["uptime_sec"], float)
    assert entry["uptime_sec"] >= 1.0


def test_status_snapshot_stdio_server_uses_command_as_url_proxy() -> None:
    """For stdio servers there's no URL — expose the command + args instead."""
    from opencomputer.mcp.client import MCPConnection, MCPManager
    from opencomputer.tools.registry import ToolRegistry

    mgr = MCPManager(tool_registry=ToolRegistry())
    cfg = MCPServerConfig(
        name="local",
        transport="stdio",
        command="python3",
        args=("-m", "my_server"),
    )
    conn = MCPConnection(config=cfg)
    conn.state = "connected"
    conn.session = MagicMock()
    mgr.connections.append(conn)

    snap = mgr.status_snapshot()
    assert len(snap) == 1
    entry = snap[0]
    # stdio servers have no URL; snapshot should present a friendly string
    assert "python3" in entry["url"]
    assert "my_server" in entry["url"]


def test_status_snapshot_disconnected_server_has_error_state() -> None:
    from opencomputer.mcp.client import MCPConnection, MCPManager
    from opencomputer.tools.registry import ToolRegistry

    mgr = MCPManager(tool_registry=ToolRegistry())
    cfg = MCPServerConfig(
        name="broken",
        transport="http",
        url="https://nope.example.com",
    )
    conn = MCPConnection(config=cfg)
    # Simulate a failed connect — no session, error captured
    conn.session = None
    conn.state = "error"
    conn.last_error = "ConnectionRefused: upstream rejected handshake"
    conn.version = None
    conn.connect_time = None
    mgr.connections.append(conn)

    snap = mgr.status_snapshot()
    assert len(snap) == 1
    entry = snap[0]
    assert entry["connection_state"] == "error"
    assert entry["last_error"] is not None
    assert "ConnectionRefused" in entry["last_error"]
    assert entry["uptime_sec"] is None
    assert entry["version"] is None
    assert entry["tool_count"] == 0


def test_status_snapshot_disconnected_state_after_shutdown() -> None:
    from opencomputer.mcp.client import MCPConnection, MCPManager
    from opencomputer.tools.registry import ToolRegistry

    mgr = MCPManager(tool_registry=ToolRegistry())
    cfg = MCPServerConfig(name="x", transport="stdio", command="echo")
    conn = MCPConnection(config=cfg)
    conn.state = "disconnected"
    conn.session = None
    conn.last_error = None
    conn.version = None
    conn.connect_time = None
    mgr.connections.append(conn)

    snap = mgr.status_snapshot()
    assert snap[0]["connection_state"] == "disconnected"
    assert snap[0]["uptime_sec"] is None


# ─── MCPConnection state lifecycle ──────────────────────────────────


async def test_connection_tracks_connected_state_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful connect → state=='connected' + connect_time set + version captured."""
    from opencomputer.mcp import client as client_mod

    class _FakeHttpCtx:
        def __init__(self, url, headers=None):
            pass

        async def __aenter__(self):
            return (MagicMock(), MagicMock(), lambda: "sid-1")

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(
        client_mod, "streamablehttp_client", lambda url, headers=None: _FakeHttpCtx(url, headers)
    )

    fake_initialize_result = MagicMock()
    fake_initialize_result.serverInfo = MagicMock(name="srv", version="4.5.6")
    fake_session = MagicMock()
    fake_session.initialize = AsyncMock(return_value=fake_initialize_result)
    fake_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(client_mod, "ClientSession", _FakeSession)

    cfg = MCPServerConfig(name="demo", transport="http", url="https://ex.com/mcp")
    conn = client_mod.MCPConnection(config=cfg)
    ok = await conn.connect()
    assert ok
    assert conn.state == "connected"
    assert conn.version == "4.5.6"
    assert conn.connect_time is not None
    assert conn.last_error is None
    await conn.disconnect()
    assert conn.state == "disconnected"


async def test_connection_tracks_error_state_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.mcp import client as client_mod

    # Unknown transport triggers the ValueError → caught → state=error.
    cfg = MCPServerConfig(name="bad", transport="websocket")
    conn = client_mod.MCPConnection(config=cfg)
    ok = await conn.connect()
    assert not ok
    assert conn.state == "error"
    assert conn.last_error is not None
    assert "websocket" in conn.last_error or "transport" in conn.last_error.lower()


# ─── CLI: opencomputer mcp status ───────────────────────────────────


def _runner_invoke(args: list[str]):
    """Isolated mcp subapp invocation — mirrors Phase 11c test helper."""
    import typer

    from opencomputer.cli_mcp import mcp_app

    root = typer.Typer()
    root.add_typer(mcp_app, name="mcp")
    return CliRunner().invoke(root, args)


def test_mcp_status_cli_renders_table_with_headers(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`opencomputer mcp status` should render a Rich table including headers + rows."""
    from opencomputer.agent.config_store import save_config

    save_config(
        Config(
            mcp=MCPConfig(
                servers=(
                    MCPServerConfig(
                        name="fake-server",
                        transport="http",
                        url="https://ex.com/mcp",
                    ),
                )
            )
        )
    )

    # Stub connect so the CLI doesn't actually hit the network.
    from opencomputer.mcp import client as client_mod

    async def _fake_connect(self, **kwargs) -> bool:  # noqa: ARG001
        self.session = MagicMock()
        self.state = "connected"
        self.version = "9.9.9"
        self.connect_time = time.monotonic()
        # Fake 2 tools
        self.tools = [
            client_mod.MCPTool(
                server_name=self.config.name,
                tool_name="t1",
                description="",
                parameters={},
                session=self.session,
            ),
            client_mod.MCPTool(
                server_name=self.config.name,
                tool_name="t2",
                description="",
                parameters={},
                session=self.session,
            ),
        ]
        return True

    async def _fake_disconnect(self) -> None:
        self.state = "disconnected"
        self.session = None

    monkeypatch.setattr(client_mod.MCPConnection, "connect", _fake_connect)
    monkeypatch.setattr(client_mod.MCPConnection, "disconnect", _fake_disconnect)

    result = _runner_invoke(["mcp", "status"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "fake-server" in out
    # Headers we expect
    assert "name" in out.lower() or "server" in out.lower()
    assert "state" in out.lower() or "status" in out.lower()
    assert "tools" in out.lower()
    # Our fake version or tool count should be rendered somewhere
    assert "9.9.9" in out or "connected" in out.lower()


def test_mcp_status_cli_empty_config_prints_hint(tmp_home: Path) -> None:
    result = _runner_invoke(["mcp", "status"])
    assert result.exit_code == 0
    assert "no MCP servers" in result.stdout or "no servers" in result.stdout.lower()
