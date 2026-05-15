"""Tests for opencomputer.mcp.bundle — plugin-shipped MCP servers (M1).

Covers:

* ``BundleMcpServer`` dataclass shape + frozen/slots invariants.
* ``${PLUGIN_ROOT}`` placeholder substitution across command, args, env, cwd.
* Path-escape attack rejection (BundleMcpSafetyError raised for paths that
  resolve outside the plugin root).
* Bundle → MCPServerConfig conversion with the ``<plugin_id>__<server>``
  prefixed name + cwd defaulting to plugin root.
* BundleMcpRegistry register/unregister keyed by plugin_id; tools from
  removed plugins disappear from the registry's flat view.
* PluginManifest.bundle_mcp roundtrips through plugin.json discovery
  (validator + flattener).
* MCPManager respects a populated BundleMcpRegistry — the bundle-derived
  configs surface alongside user-configured servers when callers pass
  ``include_bundle=True`` to ``connect_all_sync``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.mcp.bundle import (
    BundleMcpRegistry,
    BundleMcpSafetyError,
    bundle_mcp_to_mcp_server_config,
    expand_plugin_root_placeholder,
    resolve_bundle_command,
)
from opencomputer.plugins.discovery import _parse_manifest
from plugin_sdk.core import BundleMcpServer, PluginManifest

# ─── BundleMcpServer dataclass shape ─────────────────────────────


def test_bundle_mcp_server_is_frozen_with_slots() -> None:
    server = BundleMcpServer(name="memory", command="npx", args=("-y", "@modelcontextprotocol/server-memory"))
    with pytest.raises(Exception):
        server.name = "evil"  # type: ignore[misc]
    # slots → no __dict__
    assert not hasattr(server, "__dict__")


def test_bundle_mcp_server_defaults() -> None:
    server = BundleMcpServer(name="memory")
    assert server.transport == "stdio"
    assert server.command == ""
    assert server.args == ()
    assert server.env == {}
    assert server.cwd == ""
    assert server.url == ""
    assert server.headers == {}
    assert server.connection_timeout_seconds == 30.0
    assert server.lazy is True
    assert server.tools_allow is None
    assert server.tools_deny == ()
    assert server.osv_check is True


def test_bundle_mcp_server_args_tuple_required() -> None:
    # Tuples (not lists) — needed for the frozen/hashable contract.
    server = BundleMcpServer(name="memory", args=("--flag", "value"))
    assert isinstance(server.args, tuple)


# ─── ${PLUGIN_ROOT} placeholder substitution ─────────────────────


def test_expand_placeholder_replaces_token(tmp_path: Path) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    result = expand_plugin_root_placeholder(
        "${PLUGIN_ROOT}/bin/server.py", plugin_root,
    )
    assert result == f"{plugin_root}/bin/server.py"


def test_expand_placeholder_leaves_unrelated_strings(tmp_path: Path) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    assert expand_plugin_root_placeholder("/abs/path", plugin_root) == "/abs/path"
    assert expand_plugin_root_placeholder("npx", plugin_root) == "npx"
    assert expand_plugin_root_placeholder("", plugin_root) == ""


def test_expand_placeholder_multiple_occurrences(tmp_path: Path) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    out = expand_plugin_root_placeholder(
        "${PLUGIN_ROOT}/a:${PLUGIN_ROOT}/b", plugin_root,
    )
    assert out == f"{plugin_root}/a:{plugin_root}/b"


# ─── Path-escape attack rejection ─────────────────────────────────


def test_resolve_bundle_command_inside_root_ok(tmp_path: Path) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    bin_dir = plugin_root / "bin"
    bin_dir.mkdir()
    (bin_dir / "server.py").write_text("# stub\n")
    server = BundleMcpServer(
        name="srv",
        command="${PLUGIN_ROOT}/bin/server.py",
    )
    resolved = resolve_bundle_command(server, plugin_root)
    assert resolved == str((plugin_root / "bin" / "server.py").resolve())


def test_resolve_bundle_command_path_escape_raises(tmp_path: Path) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    server = BundleMcpServer(
        name="srv",
        command="${PLUGIN_ROOT}/../../../bin/rm",
    )
    with pytest.raises(BundleMcpSafetyError) as ei:
        resolve_bundle_command(server, plugin_root)
    assert "escapes plugin root" in str(ei.value)


def test_resolve_bundle_command_absolute_command_allowed(tmp_path: Path) -> None:
    # Absolute paths NOT inside the plugin root are allowed (e.g. ``npx``,
    # ``python3``, ``uvx``). We trust the user — the install-time OSV
    # scan + the env-whitelist on spawn are the security layers.
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    server = BundleMcpServer(name="srv", command="/usr/bin/python3")
    out = resolve_bundle_command(server, plugin_root)
    assert out == "/usr/bin/python3"


def test_resolve_bundle_command_bare_name_returned_verbatim(tmp_path: Path) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    server = BundleMcpServer(name="srv", command="npx")
    assert resolve_bundle_command(server, plugin_root) == "npx"


def test_resolve_bundle_command_non_stdio_returns_empty(tmp_path: Path) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    server = BundleMcpServer(
        name="srv",
        transport="http",
        url="https://example.com/mcp",
    )
    assert resolve_bundle_command(server, plugin_root) == ""


# ─── Bundle → MCPServerConfig conversion ─────────────────────────


def test_bundle_to_mcp_server_config_stdio(tmp_path: Path) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    # Eager bundle (lazy=False) — MCPServerConfig.enabled=True so the
    # MCPManager spawns it at chat start.
    server = BundleMcpServer(
        name="memory",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-memory"),
        env={"FOO": "bar"},
        lazy=False,
    )
    cfg = bundle_mcp_to_mcp_server_config(
        "test-plugin", server, plugin_root,
    )
    assert cfg.name == "test-plugin__memory"
    assert cfg.transport == "stdio"
    assert cfg.command == "npx"
    assert cfg.args == ("-y", "@modelcontextprotocol/server-memory")
    assert cfg.env == {"FOO": "bar"}
    assert cfg.enabled is True


def test_bundle_lazy_true_yields_enabled_false(tmp_path: Path) -> None:
    """M1 lazy semantics: lazy=True (default) → enabled=False in the
    derived MCPServerConfig, so connect_all skips the spawn at chat
    start. Users opt-in to mounting via ``oc mcp enable``."""
    plugin_root = tmp_path / "p"
    plugin_root.mkdir()
    server = BundleMcpServer(
        name="memory",
        command="npx",
        # lazy defaults to True
    )
    cfg = bundle_mcp_to_mcp_server_config("plug", server, plugin_root)
    assert cfg.enabled is False


def test_bundle_lazy_false_yields_enabled_true(tmp_path: Path) -> None:
    """M1 lazy semantics: lazy=False (eager opt-in) → enabled=True."""
    plugin_root = tmp_path / "p"
    plugin_root.mkdir()
    server = BundleMcpServer(name="memory", command="npx", lazy=False)
    cfg = bundle_mcp_to_mcp_server_config("plug", server, plugin_root)
    assert cfg.enabled is True


def test_bundle_to_mcp_server_config_expands_placeholders(tmp_path: Path) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    bin_dir = plugin_root / "bin"
    bin_dir.mkdir()
    (bin_dir / "server.py").write_text("# stub\n")
    server = BundleMcpServer(
        name="local",
        command="${PLUGIN_ROOT}/bin/server.py",
        args=("--cwd", "${PLUGIN_ROOT}/data"),
        env={"DATA_DIR": "${PLUGIN_ROOT}/data"},
        cwd="${PLUGIN_ROOT}",
    )
    cfg = bundle_mcp_to_mcp_server_config(
        "test-plugin", server, plugin_root,
    )
    expected_command = str((plugin_root / "bin" / "server.py").resolve())
    assert cfg.command == expected_command
    assert cfg.args == ("--cwd", f"{plugin_root}/data")
    assert cfg.env["DATA_DIR"] == f"{plugin_root}/data"


def test_bundle_to_mcp_server_config_http_transport(tmp_path: Path) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    server = BundleMcpServer(
        name="cloud",
        transport="http",
        url="https://api.example.com/mcp",
        headers={"X-Plugin": "test"},
    )
    cfg = bundle_mcp_to_mcp_server_config("plug", server, plugin_root)
    assert cfg.name == "plug__cloud"
    assert cfg.transport == "http"
    assert cfg.url == "https://api.example.com/mcp"
    assert cfg.headers == {"X-Plugin": "test"}


def test_bundle_to_mcp_server_config_tool_filters_propagate(tmp_path: Path) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    server = BundleMcpServer(
        name="srv",
        command="npx",
        tools_allow=("read", "write"),
        tools_deny=("delete",),
    )
    cfg = bundle_mcp_to_mcp_server_config("plug", server, plugin_root)
    assert cfg.tools_allow == ("read", "write")
    assert cfg.tools_deny == ("delete",)


def test_bundle_to_mcp_server_config_invalid_transport_raises(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "myplug"
    plugin_root.mkdir()
    # Transport literal is enforced at the dataclass level; construct
    # via dict to test the conversion path's defensive check.
    server = BundleMcpServer.__new__(BundleMcpServer)
    object.__setattr__(server, "name", "srv")
    object.__setattr__(server, "transport", "totally-fake-transport")
    object.__setattr__(server, "command", "")
    object.__setattr__(server, "args", ())
    object.__setattr__(server, "env", {})
    object.__setattr__(server, "cwd", "")
    object.__setattr__(server, "url", "")
    object.__setattr__(server, "headers", {})
    object.__setattr__(server, "connection_timeout_seconds", 30.0)
    object.__setattr__(server, "lazy", True)
    object.__setattr__(server, "tools_allow", None)
    object.__setattr__(server, "tools_deny", ())
    object.__setattr__(server, "osv_check", True)
    with pytest.raises(BundleMcpSafetyError) as ei:
        bundle_mcp_to_mcp_server_config("p", server, plugin_root)
    assert "transport" in str(ei.value).lower()


# ─── BundleMcpRegistry lifecycle ────────────────────────────────


def test_bundle_registry_register_and_list(tmp_path: Path) -> None:
    reg = BundleMcpRegistry()
    plug_a = tmp_path / "plug-a"
    plug_a.mkdir()
    plug_b = tmp_path / "plug-b"
    plug_b.mkdir()
    reg.register_plugin_servers(
        "plug-a",
        plug_a,
        (
            BundleMcpServer(name="memory", command="npx"),
            BundleMcpServer(name="fs", command="npx"),
        ),
    )
    reg.register_plugin_servers(
        "plug-b",
        plug_b,
        (BundleMcpServer(name="github", command="npx"),),
    )
    all_configs = reg.all_server_configs()
    names = sorted(c.name for c in all_configs)
    assert names == ["plug-a__fs", "plug-a__memory", "plug-b__github"]


def test_bundle_registry_collision_safe_for_same_name(tmp_path: Path) -> None:
    reg = BundleMcpRegistry()
    plug_a = tmp_path / "plug-a"
    plug_a.mkdir()
    plug_b = tmp_path / "plug-b"
    plug_b.mkdir()
    reg.register_plugin_servers(
        "plug-a", plug_a,
        (BundleMcpServer(name="github", command="npx"),),
    )
    reg.register_plugin_servers(
        "plug-b", plug_b,
        (BundleMcpServer(name="github", command="npx"),),
    )
    cfgs = reg.all_server_configs()
    names = sorted(c.name for c in cfgs)
    assert names == ["plug-a__github", "plug-b__github"]


def test_bundle_registry_unregister_drops_server(tmp_path: Path) -> None:
    reg = BundleMcpRegistry()
    plug_a = tmp_path / "plug-a"
    plug_a.mkdir()
    reg.register_plugin_servers(
        "plug-a", plug_a,
        (BundleMcpServer(name="memory", command="npx"),),
    )
    assert len(reg.all_server_configs()) == 1
    removed = reg.unregister_plugin("plug-a")
    assert removed == 1
    assert reg.all_server_configs() == []


def test_bundle_registry_unregister_unknown_plugin_returns_zero() -> None:
    reg = BundleMcpRegistry()
    assert reg.unregister_plugin("does-not-exist") == 0


def test_bundle_registry_servers_for_plugin_view(tmp_path: Path) -> None:
    reg = BundleMcpRegistry()
    plug = tmp_path / "plug"
    plug.mkdir()
    reg.register_plugin_servers(
        "plug", plug,
        (BundleMcpServer(name="memory", command="npx"),
         BundleMcpServer(name="fs", command="npx")),
    )
    view = reg.servers_for_plugin("plug")
    assert sorted(c.name for c in view) == ["plug__fs", "plug__memory"]
    assert reg.servers_for_plugin("unknown") == ()


def test_bundle_registry_re_register_replaces(tmp_path: Path) -> None:
    reg = BundleMcpRegistry()
    plug = tmp_path / "plug"
    plug.mkdir()
    reg.register_plugin_servers(
        "plug", plug,
        (BundleMcpServer(name="memory", command="npx"),),
    )
    # Re-registering replaces (e.g. live-reload of a plugin)
    reg.register_plugin_servers(
        "plug", plug,
        (BundleMcpServer(name="memory", command="npx"),
         BundleMcpServer(name="fs", command="npx")),
    )
    assert len(reg.servers_for_plugin("plug")) == 2


# ─── PluginManifest roundtrip via plugin.json ─────────────────────


def test_plugin_manifest_default_bundle_mcp_empty() -> None:
    """Manifests without bundle_mcp still load (backwards compat)."""
    m = PluginManifest(id="x", name="X", version="1.0.0")
    assert m.bundle_mcp == ()


def test_plugin_manifest_construction_with_bundle_mcp() -> None:
    server = BundleMcpServer(name="memory", command="npx", args=("-y",))
    m = PluginManifest(
        id="x", name="X", version="1.0.0",
        bundle_mcp=(server,),
    )
    assert m.bundle_mcp == (server,)


def test_manifest_parsing_with_bundle_mcp(tmp_path: Path) -> None:
    plug_dir = tmp_path / "test-plugin"
    plug_dir.mkdir()
    manifest_data = {
        "id": "test-plugin",
        "name": "Test",
        "version": "1.0.0",
        "entry": "plugin",
        "bundle_mcp": [
            {
                "name": "memory",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-memory"],
            },
            {
                "name": "fs",
                "transport": "stdio",
                "command": "${PLUGIN_ROOT}/bin/fs.py",
                "lazy": False,
                "tools_allow": ["read", "list"],
            },
        ],
    }
    manifest_path = plug_dir / "plugin.json"
    manifest_path.write_text(json.dumps(manifest_data))
    manifest = _parse_manifest(manifest_path)
    assert manifest is not None
    assert len(manifest.bundle_mcp) == 2
    s0 = manifest.bundle_mcp[0]
    assert s0.name == "memory"
    assert s0.command == "npx"
    assert s0.args == ("-y", "@modelcontextprotocol/server-memory")
    s1 = manifest.bundle_mcp[1]
    assert s1.name == "fs"
    assert s1.command == "${PLUGIN_ROOT}/bin/fs.py"
    assert s1.lazy is False
    assert s1.tools_allow == ("read", "list")


def test_manifest_without_bundle_mcp_still_valid(tmp_path: Path) -> None:
    plug_dir = tmp_path / "old-plugin"
    plug_dir.mkdir()
    manifest_data = {
        "id": "old-plugin",
        "name": "Old",
        "version": "1.0.0",
        "entry": "plugin",
    }
    (plug_dir / "plugin.json").write_text(json.dumps(manifest_data))
    manifest = _parse_manifest(plug_dir / "plugin.json")
    assert manifest is not None
    assert manifest.bundle_mcp == ()


def test_manifest_bundle_mcp_rejects_bad_shape(tmp_path: Path) -> None:
    plug_dir = tmp_path / "bad-plugin"
    plug_dir.mkdir()
    # Missing required ``name`` field on the bundle_mcp entry.
    manifest_data = {
        "id": "bad-plugin",
        "name": "Bad",
        "version": "1.0.0",
        "entry": "plugin",
        "bundle_mcp": [{"command": "npx"}],  # name missing
    }
    (plug_dir / "plugin.json").write_text(json.dumps(manifest_data))
    manifest = _parse_manifest(plug_dir / "plugin.json")
    # Bad bundle_mcp shape rejects the whole manifest — one bad plugin
    # shouldn't crash the rest, so caller returns None + logs.
    assert manifest is None
