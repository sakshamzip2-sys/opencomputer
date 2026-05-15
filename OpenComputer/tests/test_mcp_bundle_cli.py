"""CLI surface for bundle MCP (M1 — mcp-openclaw-port).

Validates ``oc mcp bundles`` + the bundle section in ``oc mcp list``.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_mcp import mcp_app
from opencomputer.mcp.bundle import default_registry
from plugin_sdk.core import BundleMcpServer


@pytest.fixture(autouse=True)
def _isolate_default_registry() -> Generator[None, None, None]:
    default_registry.clear()
    yield
    default_registry.clear()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_oc_mcp_bundles_empty_message(runner: CliRunner) -> None:
    result = runner.invoke(mcp_app, ["bundles"])
    assert result.exit_code == 0
    assert "no bundle MCPs" in result.stdout


def test_oc_mcp_bundles_lists_registered(
    runner: CliRunner, tmp_path: Path,
) -> None:
    plug_root = tmp_path / "plug-a"
    plug_root.mkdir()
    default_registry.register_plugin_servers(
        "plug-a",
        plug_root,
        (
            BundleMcpServer(name="memory", command="npx", args=("-y",)),
            BundleMcpServer(
                name="cloud",
                transport="http",
                url="https://api.example.com/mcp",
            ),
        ),
    )
    result = runner.invoke(mcp_app, ["bundles"])
    assert result.exit_code == 0
    out = result.stdout
    assert "plug-a" in out
    assert "memory" in out
    assert "cloud" in out
    assert "stdio" in out
    assert "http" in out


def test_oc_mcp_list_includes_bundles_section(
    runner: CliRunner, tmp_path: Path,
) -> None:
    plug_root = tmp_path / "plug-a"
    plug_root.mkdir()
    default_registry.register_plugin_servers(
        "plug-a",
        plug_root,
        (BundleMcpServer(name="memory", command="npx"),),
    )
    result = runner.invoke(mcp_app, ["list"])
    assert result.exit_code == 0
    # The bundles table title includes "bundled — plugin 'plug-a'"
    assert "bundled" in result.stdout
    assert "plug-a" in result.stdout
