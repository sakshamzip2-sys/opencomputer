"""Smoke test for the downloads-cleanup-mcp reference plugin (M5).

End-to-end:

* Discover the plugin via the standard discovery path.
* Verify the manifest's ``bundle_mcp`` round-trips to a ``BundleMcpServer``
  with the right shape (``${PLUGIN_ROOT}/mcp_server.py`` placeholder
  intact pre-substitution).
* Register through the loader hook and confirm the resulting
  ``MCPServerConfig`` resolves the placeholder to an absolute path
  inside the plugin tree.

The actual MCP server is not spawned here (that's the
``oc plugins activate`` / live-chat smoke test surface).
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from opencomputer.mcp.bundle import BundleMcpRegistry
from opencomputer.plugins.discovery import _parse_manifest
from opencomputer.plugins.loader import _register_bundle_mcps
from plugin_sdk.core import PluginManifest


@pytest.fixture
def plugin_root() -> Path:
    """Resolve to the bundled reference plugin directory."""
    return (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "downloads-cleanup-mcp"
    )


def test_reference_plugin_directory_exists(plugin_root: Path) -> None:
    assert plugin_root.is_dir()
    assert (plugin_root / "plugin.json").exists()
    assert (plugin_root / "plugin.py").exists()
    assert (plugin_root / "mcp_server.py").exists()


def test_reference_plugin_manifest_parses(plugin_root: Path) -> None:
    manifest = _parse_manifest(plugin_root / "plugin.json")
    assert isinstance(manifest, PluginManifest)
    assert manifest.id == "downloads-cleanup-mcp"
    assert manifest.version == "0.1.0"
    assert manifest.kind == "tool"
    assert len(manifest.bundle_mcp) == 1
    bm = manifest.bundle_mcp[0]
    assert bm.name == "downloads"
    assert bm.transport == "stdio"
    assert bm.command == "${PLUGIN_ROOT}/mcp_server.py"
    assert bm.lazy is True
    # osv_check explicitly off for a plugin-shipped server (we trust the
    # plugin author; install-time review is the safety boundary).
    assert bm.osv_check is False


@pytest.fixture
def isolated_registry(
    plugin_root: Path,
) -> Generator[BundleMcpRegistry, None, None]:
    reg = BundleMcpRegistry()
    yield reg
    reg.clear()


def test_reference_plugin_registers_into_bundle_registry(
    plugin_root: Path,
    isolated_registry: BundleMcpRegistry,
) -> None:
    from opencomputer.plugins.discovery import PluginCandidate

    manifest = _parse_manifest(plugin_root / "plugin.json")
    assert manifest is not None
    cand = PluginCandidate(
        manifest=manifest,
        root_dir=plugin_root,
        manifest_path=plugin_root / "plugin.json",
    )
    n = _register_bundle_mcps(cand, registry=isolated_registry)
    assert n == 1
    configs = isolated_registry.servers_for_plugin("downloads-cleanup-mcp")
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.name == "downloads-cleanup-mcp__downloads"
    expected = str((plugin_root / "mcp_server.py").resolve())
    assert cfg.command == expected
    assert cfg.transport == "stdio"


def test_reference_plugin_mcp_server_is_executable(plugin_root: Path) -> None:
    import os
    server = plugin_root / "mcp_server.py"
    assert server.exists()
    # Python file — should be readable + (since we chmod'd) executable.
    assert os.access(server, os.R_OK)
    # On unix, the exec bit should be set so the bundle can spawn it.
    assert os.access(server, os.X_OK)


def test_reference_plugin_mcp_server_imports_cleanly(plugin_root: Path) -> None:
    """The server file imports without spawning. We don't run it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_test_downloads_cleanup_mcp_server",
        plugin_root / "mcp_server.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Confirm the symbols are exported as expected.
    assert hasattr(mod, "build_server")
    assert hasattr(mod, "main")
    server = mod.build_server()
    # FastMCP exposes list_tools — verify our three tools are registered.
    import asyncio
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "list_downloads" in names
    assert "summarise_downloads" in names
    assert "archive_old" in names
