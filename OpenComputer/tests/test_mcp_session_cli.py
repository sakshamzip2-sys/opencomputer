"""CLI surface for per-session MCP runtimes (M2 — mcp-openclaw-port).

Validates ``oc mcp sessions`` rendering paths:

* config flag off → friendly opt-in hint.
* config flag on, no agent in this process → "no active runtime" message.
* config flag on + runtime registered + sessions present → table.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli_mcp import mcp_app
from opencomputer.mcp.session_registry import (
    current_runtime_manager,
    set_runtime_manager,
)
from opencomputer.mcp.session_runtime import (
    SessionMcpRuntimeManager,
)


@pytest.fixture(autouse=True)
def _isolate_runtime() -> Generator[None, None, None]:
    prior = current_runtime_manager()
    set_runtime_manager(None)
    yield
    set_runtime_manager(prior)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _config_with_session_scoped(enabled: bool):
    """Return a Config-like object with mcp.session_scoped set."""
    from opencomputer.agent.config import Config, MCPConfig
    return Config(mcp=replace(MCPConfig(), session_scoped=enabled))


def test_sessions_cli_when_flag_off(runner: CliRunner) -> None:
    with patch(
        "opencomputer.agent.config_store.load_config",
        return_value=_config_with_session_scoped(False),
    ):
        result = runner.invoke(mcp_app, ["sessions"])
    assert result.exit_code == 0
    assert "session_scoped MCP is disabled" in result.stdout


def test_sessions_cli_when_flag_on_no_runtime(runner: CliRunner) -> None:
    with patch(
        "opencomputer.agent.config_store.load_config",
        return_value=_config_with_session_scoped(True),
    ):
        result = runner.invoke(mcp_app, ["sessions"])
    assert result.exit_code == 0
    assert "no active session-runtime" in result.stdout


def test_sessions_cli_renders_table(runner: CliRunner) -> None:
    """With a registered runtime and active session, the table renders."""
    factory_mock = MagicMock(return_value=MagicMock(connections=[]))
    rt = SessionMcpRuntimeManager(mcp_manager_factory=factory_mock)
    rt.get_or_create("sess-1")
    rt.get_or_create("sess-2")
    set_runtime_manager(rt)

    with patch(
        "opencomputer.agent.config_store.load_config",
        return_value=_config_with_session_scoped(True),
    ):
        result = runner.invoke(mcp_app, ["sessions"])

    assert result.exit_code == 0
    assert "sess-1" in result.stdout
    assert "sess-2" in result.stdout


def test_session_registry_set_and_get() -> None:
    factory_mock = MagicMock(return_value=MagicMock(connections=[]))
    rt = SessionMcpRuntimeManager(mcp_manager_factory=factory_mock)
    set_runtime_manager(rt)
    assert current_runtime_manager() is rt
    set_runtime_manager(None)
    assert current_runtime_manager() is None
