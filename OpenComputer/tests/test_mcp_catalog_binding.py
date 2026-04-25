"""Tests for G.11 / Tier 2.13 — plugin manifest ``mcp_servers`` auto-installs MCP presets.

Verifies the discovery → loader → config-write flow:

- Manifest validator accepts ``mcp_servers`` array of preset slugs.
- _parse_manifest threads the field into ``PluginManifest.mcp_servers``.
- After register() succeeds, the loader installs each preset into
  ``config.yaml`` (idempotent — skips if name exists; warns on unknown slug).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.agent.config_store import load_config
from opencomputer.plugins.discovery import _parse_manifest
from opencomputer.plugins.loader import _install_mcp_servers_from_manifest
from opencomputer.plugins.manifest_validator import validate_manifest
from plugin_sdk.core import PluginManifest


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Schema validator + manifest parsing
# ---------------------------------------------------------------------------


class TestManifestSchema:
    def test_validator_accepts_mcp_servers(self) -> None:
        schema, err = validate_manifest({
            "id": "test-plugin",
            "name": "Test",
            "version": "0.1.0",
            "entry": "plugin",
            "kind": "tool",
            "mcp_servers": ["filesystem", "github"],
        })
        assert err == ""
        assert schema is not None
        assert schema.mcp_servers == ["filesystem", "github"]

    def test_validator_default_empty(self) -> None:
        schema, _ = validate_manifest({
            "id": "test-plugin",
            "name": "Test",
            "version": "0.1.0",
            "entry": "plugin",
            "kind": "tool",
        })
        assert schema.mcp_servers == []

    def test_parse_manifest_threads_field(self, tmp_path: Path) -> None:
        manifest_file = tmp_path / "plugin.json"
        manifest_file.write_text(
            json.dumps({
                "id": "test-plugin",
                "name": "Test",
                "version": "0.1.0",
                "entry": "plugin",
                "kind": "tool",
                "mcp_servers": ["fetch"],
            })
        )
        m = _parse_manifest(manifest_file)
        assert m is not None
        assert m.mcp_servers == ("fetch",)


# ---------------------------------------------------------------------------
# Loader integration — install MCP from manifest
# ---------------------------------------------------------------------------


class TestInstallMCPFromManifest:
    def test_installs_known_preset(self) -> None:
        m = PluginManifest(
            id="x",
            name="X",
            version="0.1.0",
            mcp_servers=("filesystem",),
        )
        _install_mcp_servers_from_manifest(m)

        cfg = load_config()
        names = [s.name for s in cfg.mcp.servers]
        assert "filesystem" in names

    def test_idempotent(self) -> None:
        m = PluginManifest(
            id="x",
            name="X",
            version="0.1.0",
            mcp_servers=("filesystem",),
        )
        _install_mcp_servers_from_manifest(m)
        _install_mcp_servers_from_manifest(m)  # Run again

        cfg = load_config()
        # filesystem appears exactly once
        assert sum(1 for s in cfg.mcp.servers if s.name == "filesystem") == 1

    def test_unknown_slug_warns_but_continues(self, caplog: pytest.LogCaptureFixture) -> None:
        m = PluginManifest(
            id="x",
            name="X",
            version="0.1.0",
            mcp_servers=("nonsense", "filesystem"),
        )
        with caplog.at_level("WARNING"):
            _install_mcp_servers_from_manifest(m)

        # filesystem still installed
        cfg = load_config()
        assert any(s.name == "filesystem" for s in cfg.mcp.servers)
        # And we logged a warning about the unknown slug
        assert any("nonsense" in r.message for r in caplog.records)

    def test_multiple_presets_at_once(self) -> None:
        m = PluginManifest(
            id="x",
            name="X",
            version="0.1.0",
            mcp_servers=("filesystem", "fetch", "github"),
        )
        _install_mcp_servers_from_manifest(m)

        cfg = load_config()
        names = {s.name for s in cfg.mcp.servers}
        assert {"filesystem", "fetch", "github"} <= names

    def test_empty_list_no_op(self) -> None:
        m = PluginManifest(id="x", name="X", version="0.1.0", mcp_servers=())
        cfg_before = load_config()
        _install_mcp_servers_from_manifest(m)
        cfg_after = load_config()
        assert len(cfg_before.mcp.servers) == len(cfg_after.mcp.servers)

    def test_user_customisation_respected(self) -> None:
        """If the user already has a server with the preset's name, don't clobber it."""
        import dataclasses

        from opencomputer.agent.config import MCPServerConfig
        from opencomputer.agent.config_store import save_config

        # Pre-populate with a custom filesystem entry
        cfg = load_config()
        custom = MCPServerConfig(
            name="filesystem",
            transport="stdio",
            command="/usr/local/bin/my-custom-fs",
            args=("--root", "/special"),
            url="",
            env={"CUSTOM": "1"},
            headers={},
            enabled=True,
        )
        cfg = dataclasses.replace(
            cfg, mcp=dataclasses.replace(cfg.mcp, servers=(custom,))
        )
        save_config(cfg)

        m = PluginManifest(
            id="x",
            name="X",
            version="0.1.0",
            mcp_servers=("filesystem",),
        )
        _install_mcp_servers_from_manifest(m)

        cfg_after = load_config()
        # User's custom command preserved
        fs_servers = [s for s in cfg_after.mcp.servers if s.name == "filesystem"]
        assert len(fs_servers) == 1
        assert fs_servers[0].command == "/usr/local/bin/my-custom-fs"
