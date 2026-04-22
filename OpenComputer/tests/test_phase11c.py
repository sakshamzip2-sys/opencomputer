"""Phase 11c: MCP expansion — HTTP/SSE transports + opencomputer mcp CLI.

Tests cover:
- MCPServerConfig accepts the new headers field + sse / http transports.
- MCPConnection.connect routes to sse_client / streamablehttp_client based
  on transport (mocked — no real network).
- MCPConnection.connect raises a friendly ValueError when sse/http transport
  is configured without a url.
- `opencomputer mcp add / list / remove / enable / disable` round-trips
  through ~/.opencomputer/config.yaml correctly.
- `opencomputer mcp add` rejects unknown transports + missing required flags.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from opencomputer.agent.config import Config, MCPConfig, MCPServerConfig
from opencomputer.agent.config_store import load_config, save_config


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _home() so tests don't touch the real ~/.opencomputer."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


# ─── MCPServerConfig surface ───────────────────────────────────────────


def test_mcp_server_config_supports_headers_and_three_transports() -> None:
    cfg = MCPServerConfig(
        name="x",
        transport="sse",
        url="https://example.com/sse",
        headers={"Authorization": "Bearer xyz"},
    )
    assert cfg.transport == "sse"
    assert cfg.headers == {"Authorization": "Bearer xyz"}
    assert cfg.url == "https://example.com/sse"


def test_mcp_server_config_default_transport_is_stdio() -> None:
    cfg = MCPServerConfig(name="x", command="echo")
    assert cfg.transport == "stdio"
    assert cfg.headers == {}


# ─── MCPConnection transport routing ───────────────────────────────────


async def test_mcp_connection_sse_transport_calls_sse_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.mcp import client as client_mod

    captured: dict = {}

    class _FakeCtx:
        def __init__(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers

        async def __aenter__(self):
            return (MagicMock(), MagicMock())

        async def __aexit__(self, *a):
            return None

    captured["sse_calls"] = 0

    def _fake_sse(url, headers=None):
        captured["sse_calls"] += 1
        return _FakeCtx(url, headers)

    monkeypatch.setattr(client_mod, "sse_client", _fake_sse)

    fake_session = MagicMock()
    fake_session.initialize = AsyncMock()
    fake_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(client_mod, "ClientSession", _FakeSession)

    cfg = MCPServerConfig(
        name="hosted",
        transport="sse",
        url="https://x.example.com/sse",
        headers={"Authorization": "Bearer t"},
    )
    conn = client_mod.MCPConnection(config=cfg)
    ok = await conn.connect()
    assert ok
    assert captured["sse_calls"] == 1
    assert captured["url"] == "https://x.example.com/sse"
    assert captured["headers"] == {"Authorization": "Bearer t"}
    await conn.disconnect()


async def test_mcp_connection_http_transport_calls_streamablehttp_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.mcp import client as client_mod

    captured: dict = {"http_calls": 0}

    class _FakeCtx:
        def __init__(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers

        async def __aenter__(self):
            # streamablehttp returns 3 streams (read, write, get_session_id)
            return (MagicMock(), MagicMock(), lambda: "sid-1")

        async def __aexit__(self, *a):
            return None

    def _fake_http(url, headers=None):
        captured["http_calls"] += 1
        return _FakeCtx(url, headers)

    monkeypatch.setattr(client_mod, "streamablehttp_client", _fake_http)

    fake_session = MagicMock()
    fake_session.initialize = AsyncMock()
    fake_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(client_mod, "ClientSession", _FakeSession)

    cfg = MCPServerConfig(
        name="modern",
        transport="http",
        url="https://x.example.com/v1",
        headers={"X-Token": "abc"},
    )
    conn = client_mod.MCPConnection(config=cfg)
    ok = await conn.connect()
    assert ok
    assert captured["http_calls"] == 1
    assert captured["url"] == "https://x.example.com/v1"
    assert captured["headers"] == {"X-Token": "abc"}
    await conn.disconnect()


async def test_mcp_connection_sse_without_url_returns_false_and_logs() -> None:
    from opencomputer.mcp import client as client_mod

    cfg = MCPServerConfig(name="bad", transport="sse", url="")
    conn = client_mod.MCPConnection(config=cfg)
    # ValueError is caught by the broad except in connect — returns False.
    ok = await conn.connect()
    assert not ok


async def test_mcp_connection_unknown_transport_returns_false() -> None:
    from opencomputer.mcp import client as client_mod

    cfg = MCPServerConfig(name="bad", transport="websocket")
    conn = client_mod.MCPConnection(config=cfg)
    ok = await conn.connect()
    assert not ok


# ─── opencomputer mcp CLI round-trip ───────────────────────────────────


def _runner_invoke(args: list[str]):
    """Build the `opencomputer mcp` subapp standalone so tests don't need to
    touch the full opencomputer typer (which imports MCPManager etc.)."""
    import typer

    from opencomputer.cli_mcp import mcp_app

    root = typer.Typer()
    root.add_typer(mcp_app, name="mcp")
    return CliRunner().invoke(root, args)


def test_mcp_cli_add_then_list_round_trips(tmp_home: Path) -> None:
    # Start with no config
    result = _runner_invoke(["mcp", "list"])
    assert result.exit_code == 0
    assert "no MCP servers" in result.stdout

    # Add a stdio server
    result = _runner_invoke(
        [
            "mcp",
            "add",
            "filesystem",
            "--transport",
            "stdio",
            "--command",
            "npx",
            "--arg",
            "-y",
            "--arg",
            "@modelcontextprotocol/server-filesystem",
            "--arg",
            "/tmp/sandbox",
        ]
    )
    assert result.exit_code == 0, result.stdout
    assert "added" in result.stdout

    # List should show it
    result = _runner_invoke(["mcp", "list"])
    assert result.exit_code == 0
    assert "filesystem" in result.stdout
    assert "stdio" in result.stdout

    # Underlying YAML actually changed
    cfg = load_config()
    assert any(s.name == "filesystem" for s in cfg.mcp.servers)


def test_mcp_cli_add_sse_with_headers(tmp_home: Path) -> None:
    result = _runner_invoke(
        [
            "mcp",
            "add",
            "remote",
            "--transport",
            "sse",
            "--url",
            "https://mcp.example.com/sse",
            "--header",
            "Authorization=Bearer abc",
            "--header",
            "X-Trace=on",
        ]
    )
    assert result.exit_code == 0
    cfg = load_config()
    server = next(s for s in cfg.mcp.servers if s.name == "remote")
    assert server.transport == "sse"
    assert server.url == "https://mcp.example.com/sse"
    assert server.headers == {"Authorization": "Bearer abc", "X-Trace": "on"}


def test_mcp_cli_add_rejects_unknown_transport(tmp_home: Path) -> None:
    result = _runner_invoke(["mcp", "add", "x", "--transport", "websocket", "--url", "wss://x"])
    assert result.exit_code != 0
    out = result.stdout + (result.stderr if hasattr(result, "stderr") else "")
    assert "transport must be" in out or "transport" in out.lower()


def test_mcp_cli_add_rejects_stdio_without_command(tmp_home: Path) -> None:
    result = _runner_invoke(["mcp", "add", "x", "--transport", "stdio"])
    assert result.exit_code != 0
    assert (
        "command"
        in (result.stdout + result.stderr if hasattr(result, "stderr") else result.output).lower()
    )


def test_mcp_cli_add_rejects_sse_without_url(tmp_home: Path) -> None:
    result = _runner_invoke(["mcp", "add", "x", "--transport", "sse"])
    assert result.exit_code != 0
    assert (
        "url"
        in (result.stdout + result.stderr if hasattr(result, "stderr") else result.output).lower()
    )


def test_mcp_cli_add_rejects_duplicate_name(tmp_home: Path) -> None:
    save_config(Config(mcp=MCPConfig(servers=(MCPServerConfig(name="dup", command="echo"),))))
    result = _runner_invoke(["mcp", "add", "dup", "--transport", "stdio", "--command", "echo"])
    assert result.exit_code != 0
    assert "already exists" in result.stdout


def test_mcp_cli_remove(tmp_home: Path) -> None:
    save_config(Config(mcp=MCPConfig(servers=(MCPServerConfig(name="goner", command="echo"),))))
    result = _runner_invoke(["mcp", "remove", "goner"])
    assert result.exit_code == 0
    assert "removed" in result.stdout
    cfg = load_config()
    assert not any(s.name == "goner" for s in cfg.mcp.servers)


def test_mcp_cli_enable_disable(tmp_home: Path) -> None:
    save_config(
        Config(mcp=MCPConfig(servers=(MCPServerConfig(name="t", command="echo", enabled=False),)))
    )
    result = _runner_invoke(["mcp", "enable", "t"])
    assert result.exit_code == 0
    assert load_config().mcp.servers[0].enabled is True

    result = _runner_invoke(["mcp", "disable", "t"])
    assert result.exit_code == 0
    assert load_config().mcp.servers[0].enabled is False


def test_mcp_cli_remove_unknown_returns_nonzero(tmp_home: Path) -> None:
    result = _runner_invoke(["mcp", "remove", "ghost"])
    assert result.exit_code != 0
    assert "not found" in result.stdout
