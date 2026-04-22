"""Phase 14.E — Profile-local plugins dir + install/uninstall CLI flags.

Two behaviors:

1. Discovery scans three roots in priority order — profile-local,
   global, bundled — and profile-local shadows global shadows bundled
   on id collision (handled by ``discovery.py::discover`` which is
   already first-wins by scan order).

2. ``opencomputer plugin install <path> [--profile X] [--global]``
   copies a plugin directory into the chosen root.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner


def _runner():
    return CliRunner()


def _isolated(tmp_path, monkeypatch):
    """Point OPENCOMPUTER_HOME_ROOT at tmp_path; return the plugin_app."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    import importlib

    from opencomputer import cli_plugin

    importlib.reload(cli_plugin)
    return cli_plugin.plugin_app


def _write_stub_plugin(dest: Path, plugin_id: str) -> Path:
    """Create a minimal plugin dir at dest/plugin_id/ with plugin.json + stub."""
    d = dest / plugin_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id.title(),
                "version": "0.1.0",
                "entry": "plugin",
                "kind": "mixed",
            }
        )
    )
    (d / "plugin.py").write_text("def register(api):\n    pass\n")
    return d


class TestPluginInstall:
    def test_install_defaults_to_profile_local_when_active(self, tmp_path, monkeypatch):
        # Set active profile = coder so the default install target is
        # ~/.opencomputer/profiles/coder/plugins/
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        (tmp_path / "active_profile").write_text("coder\n")

        src = tmp_path / "src"
        _write_stub_plugin(src, "my-tool")
        app = _isolated(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["install", str(src / "my-tool")])
        assert result.exit_code == 0, result.stdout
        assert (tmp_path / "profiles" / "coder" / "plugins" / "my-tool" / "plugin.json").exists()

    def test_install_with_global_flag(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        (tmp_path / "active_profile").write_text("coder\n")

        src = tmp_path / "src"
        _write_stub_plugin(src, "shared")
        app = _isolated(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["install", str(src / "shared"), "--global"])
        assert result.exit_code == 0, result.stdout
        assert (tmp_path / "plugins" / "shared" / "plugin.json").exists()
        # Should NOT land in profile-local
        assert not (tmp_path / "profiles" / "coder" / "plugins" / "shared" / "plugin.json").exists()

    def test_install_with_explicit_profile(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "stocks").mkdir(parents=True)
        src = tmp_path / "src"
        _write_stub_plugin(src, "trader")
        app = _isolated(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["install", str(src / "trader"), "--profile", "stocks"])
        assert result.exit_code == 0, result.stdout
        assert (tmp_path / "profiles" / "stocks" / "plugins" / "trader" / "plugin.json").exists()

    def test_install_refuses_existing_without_force(self, tmp_path, monkeypatch):
        # Active = default, so install goes to ~/.opencomputer/plugins/
        src = tmp_path / "src"
        _write_stub_plugin(src, "dup")
        # Pre-existing at the default target
        (tmp_path / "plugins" / "dup").mkdir(parents=True)
        app = _isolated(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["install", str(src / "dup")])
        assert result.exit_code != 0
        assert "already exists" in result.stdout.lower() or "force" in result.stdout.lower()

    def test_install_with_force_overwrites(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        _write_stub_plugin(src, "dup")
        existing = tmp_path / "plugins" / "dup"
        existing.mkdir(parents=True)
        (existing / "old.txt").write_text("old")
        app = _isolated(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["install", str(src / "dup"), "--force"])
        assert result.exit_code == 0
        assert not (existing / "old.txt").exists()
        assert (existing / "plugin.json").exists()

    def test_install_source_without_manifest_errors(self, tmp_path, monkeypatch):
        src = tmp_path / "bogus"
        src.mkdir()
        (src / "readme.txt").write_text("not a plugin")
        app = _isolated(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["install", str(src)])
        assert result.exit_code != 0
        assert "plugin.json" in result.stdout.lower()


class TestPluginUninstall:
    def test_uninstall_profile_local(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        (tmp_path / "active_profile").write_text("coder\n")
        _write_stub_plugin(tmp_path / "profiles" / "coder" / "plugins", "my-tool")
        app = _isolated(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["uninstall", "my-tool", "--yes"])
        assert result.exit_code == 0
        assert not (tmp_path / "profiles" / "coder" / "plugins" / "my-tool").exists()

    def test_uninstall_missing_plugin_errors(self, tmp_path, monkeypatch):
        app = _isolated(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["uninstall", "nothing", "--yes"])
        assert result.exit_code != 0


class TestPluginWhere:
    def test_where_finds_profile_local(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        (tmp_path / "active_profile").write_text("coder\n")
        _write_stub_plugin(tmp_path / "profiles" / "coder" / "plugins", "my-tool")
        app = _isolated(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["where", "my-tool"])
        assert result.exit_code == 0
        assert "profiles/coder/plugins/my-tool" in result.stdout

    def test_where_errors_when_missing(self, tmp_path, monkeypatch):
        app = _isolated(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["where", "nothing"])
        assert result.exit_code != 0


class TestDiscoveryPriority:
    """Profile-local > global > bundled on id collision."""

    def test_discovery_includes_profile_local_before_global(self, tmp_path, monkeypatch):
        """A plugin that exists in BOTH profile-local and global
        resolves to profile-local because profile-local is scanned first."""
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        (tmp_path / "active_profile").write_text("coder\n")

        # Same plugin id "shared" in two locations
        _write_stub_plugin(tmp_path / "profiles" / "coder" / "plugins", "shared")
        _write_stub_plugin(tmp_path / "plugins", "shared")

        # Mark each file so we can tell which one wins
        (tmp_path / "profiles" / "coder" / "plugins" / "shared" / "marker.txt").write_text(
            "profile-local"
        )
        (tmp_path / "plugins" / "shared" / "marker.txt").write_text("global")

        # Reload cli to pick up new env
        import importlib

        from opencomputer import cli as cli_mod

        importlib.reload(cli_mod)

        from opencomputer.plugins.discovery import discover

        search_paths = [
            tmp_path / "profiles" / "coder" / "plugins",
            tmp_path / "plugins",
        ]
        candidates = discover(search_paths)
        shared = [c for c in candidates if c.manifest.id == "shared"]
        assert len(shared) == 1, f"discovery should dedupe: got {len(shared)} results"
        # The winning candidate should be the profile-local one
        marker = (shared[0].root_dir / "marker.txt").read_text()
        assert marker == "profile-local"
