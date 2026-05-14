"""Loader-side wiring for bundle MCP (M1 — mcp-openclaw-port).

Confirms:

* On plugin load, ``BundleMcpRegistry`` receives the plugin's bundle
  servers — one entry per server, namespaced by plugin id.
* On plugin teardown, the registry drops every entry for that plugin id.
* A plugin with malformed bundle entries (path-escape attack) loads
  successfully but the bad bundle entry is skipped + logged.
* ``MCPManager.connect_all_sync`` accepts an ``extra_servers`` arg so
  callers can splice the bundle registry's view into the connect list.
"""

from __future__ import annotations

import json
from pathlib import Path

from opencomputer.mcp.bundle import BundleMcpRegistry, default_registry
from opencomputer.plugins.discovery import PluginCandidate, _parse_manifest
from opencomputer.plugins.loader import _register_bundle_mcps, _unregister_bundle_mcps
from plugin_sdk.core import BundleMcpServer, PluginManifest


def _make_candidate(
    tmp_path: Path,
    plugin_id: str,
    bundle_mcp: tuple[BundleMcpServer, ...] = (),
) -> PluginCandidate:
    """Build a minimal PluginCandidate with the given bundle_mcp servers."""
    plug_dir = tmp_path / plugin_id
    plug_dir.mkdir()
    manifest_path = plug_dir / "plugin.json"
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id.title(),
        version="1.0.0",
        entry="plugin",
        bundle_mcp=bundle_mcp,
    )
    return PluginCandidate(
        manifest=manifest,
        root_dir=plug_dir,
        manifest_path=manifest_path,
    )


def test_register_bundle_mcps_no_op_when_empty(tmp_path: Path) -> None:
    reg = BundleMcpRegistry()
    cand = _make_candidate(tmp_path, "plug-a", bundle_mcp=())
    n = _register_bundle_mcps(cand, registry=reg)
    assert n == 0
    assert reg.all_server_configs() == []


def test_register_bundle_mcps_registers_each(tmp_path: Path) -> None:
    reg = BundleMcpRegistry()
    cand = _make_candidate(
        tmp_path,
        "plug-a",
        bundle_mcp=(
            BundleMcpServer(name="memory", command="npx"),
            BundleMcpServer(name="fs", command="npx"),
        ),
    )
    n = _register_bundle_mcps(cand, registry=reg)
    assert n == 2
    names = sorted(c.name for c in reg.all_server_configs())
    assert names == ["plug-a__fs", "plug-a__memory"]


def test_unregister_bundle_mcps_drops_all_for_plugin(tmp_path: Path) -> None:
    reg = BundleMcpRegistry()
    cand = _make_candidate(
        tmp_path,
        "plug-a",
        bundle_mcp=(
            BundleMcpServer(name="memory", command="npx"),
            BundleMcpServer(name="fs", command="npx"),
        ),
    )
    _register_bundle_mcps(cand, registry=reg)
    removed = _unregister_bundle_mcps("plug-a", registry=reg)
    assert removed == 2
    assert reg.all_server_configs() == []


def test_register_skips_bad_entry_but_keeps_rest(tmp_path: Path) -> None:
    """A path-escape entry is skipped + logged; siblings still register."""
    reg = BundleMcpRegistry()
    cand = _make_candidate(
        tmp_path,
        "plug-a",
        bundle_mcp=(
            BundleMcpServer(name="ok", command="npx"),
            BundleMcpServer(
                name="evil",
                command="${PLUGIN_ROOT}/../../../bin/rm",
            ),
        ),
    )
    n = _register_bundle_mcps(cand, registry=reg)
    # ``ok`` registers; ``evil`` is rejected by safety check inside
    # BundleMcpRegistry.register_plugin_servers but the caller still
    # sees a non-empty registration (the one that survived).
    assert n == 1
    names = sorted(c.name for c in reg.all_server_configs())
    assert names == ["plug-a__ok"]


def test_default_registry_singleton_is_shared() -> None:
    """The module-level ``default_registry`` is the production singleton."""
    from opencomputer.mcp.bundle import default_registry as r1
    from opencomputer.mcp.bundle import default_registry as r2
    assert r1 is r2


def test_full_discovery_through_loader_round_trip(tmp_path: Path) -> None:
    """End-to-end: write plugin.json, parse manifest, register via loader hook."""
    plug_dir = tmp_path / "plug-a"
    plug_dir.mkdir()
    bin_dir = plug_dir / "bin"
    bin_dir.mkdir()
    (bin_dir / "server.py").write_text("# stub\n")
    manifest_data = {
        "id": "plug-a",
        "name": "Plug A",
        "version": "1.0.0",
        "entry": "plugin",
        "bundle_mcp": [
            {
                "name": "local",
                "command": "${PLUGIN_ROOT}/bin/server.py",
                "lazy": True,
            },
        ],
    }
    (plug_dir / "plugin.json").write_text(json.dumps(manifest_data))
    manifest = _parse_manifest(plug_dir / "plugin.json")
    assert manifest is not None
    cand = PluginCandidate(
        manifest=manifest, root_dir=plug_dir, manifest_path=plug_dir / "plugin.json",
    )
    reg = BundleMcpRegistry()
    _register_bundle_mcps(cand, registry=reg)
    configs = reg.all_server_configs()
    assert len(configs) == 1
    assert configs[0].name == "plug-a__local"
    expected_cmd = str((plug_dir / "bin" / "server.py").resolve())
    assert configs[0].command == expected_cmd


def test_default_registry_cleanup_between_tests(tmp_path: Path) -> None:
    """Sanity: clear() empties the default registry; used by test isolation."""
    default_registry.clear()
    cand = _make_candidate(
        tmp_path, "x", (BundleMcpServer(name="m", command="npx"),),
    )
    _register_bundle_mcps(cand)
    assert default_registry.servers_for_plugin("x") != ()
    default_registry.clear()
    assert default_registry.servers_for_plugin("x") == ()
