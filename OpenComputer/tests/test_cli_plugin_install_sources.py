"""Tests for the new git+/https:// install routing + `oc plugin verify`."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from opencomputer.cli_plugin import plugin_app
from tests._helpers.install_fixtures import make_tarball as _make_tarball


def _write_permissive_policy(home: Path) -> None:
    """v1.1 plan-3 M11.3: every install path runs through
    ``_enforce_source_policy``.  Pre-M11.3 tests that call install with
    git+/https://... arguments need to opt into the new policy world by
    writing a permissive config.yaml.  Without this every install would
    be denied by the deny-by-default-on-network policy."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "plugins:\n"
        "  sources:\n"
        "    git: {allow: ['*']}\n"
        "    url: {allow: ['*']}\n"
        "    github: {allow: ['*']}\n"
        "    pypi: {allow: ['*']}\n",
        encoding="utf-8",
    )


def test_install_arg_starting_with_git_routes_to_git(
    tmp_path: Path, monkeypatch
):
    """`oc plugin install git+https://...` calls install_from_git, not local copy."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    _write_permissive_policy(tmp_path / "home")
    captured: dict = {}

    def fake_git_install(url, *, dest_root, plugin_id_hint, **kwargs):
        captured["url"] = url
        captured["plugin_id_hint"] = plugin_id_hint
        from opencomputer.plugins.remote_install import InstallResult

        plugin_dir = dest_root / plugin_id_hint
        plugin_dir.mkdir(parents=True, exist_ok=True)
        return InstallResult(plugin_id_hint, "0.1.0", plugin_dir)

    runner = CliRunner()
    with patch(
        "opencomputer.cli_plugin._install_from_git", side_effect=fake_git_install
    ):
        result = runner.invoke(
            plugin_app,
            [
                "install",
                "git+https://github.com/example/foo.git",
                "--id",
                "foo",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured["plugin_id_hint"] == "foo"
    assert captured["url"] == "git+https://github.com/example/foo.git"


def test_install_git_without_id_errors(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    _write_permissive_policy(tmp_path / "home")
    runner = CliRunner()
    result = runner.invoke(
        plugin_app, ["install", "git+https://github.com/example/foo.git"]
    )
    assert result.exit_code == 2, result.output
    assert "--id" in result.output


def test_install_arg_starting_with_https_routes_to_url(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    _write_permissive_policy(tmp_path / "home")
    raw = _make_tarball("urlcli")
    sha = hashlib.sha256(raw).hexdigest()

    def fake_url_install(
        url, *, dest_root, plugin_id_hint, sha256, **kwargs
    ):
        from opencomputer.plugins.remote_install import InstallResult

        plugin_dir = dest_root / plugin_id_hint
        plugin_dir.mkdir(parents=True, exist_ok=True)
        return InstallResult(plugin_id_hint, "0.1.0", plugin_dir)

    runner = CliRunner()
    with patch(
        "opencomputer.cli_plugin._install_from_url",
        side_effect=fake_url_install,
    ):
        result = runner.invoke(
            plugin_app,
            [
                "install",
                "https://example.com/x.tgz",
                "--id",
                "urlcli",
                "--sha256",
                sha,
            ],
        )
    assert result.exit_code == 0, result.output


def test_install_url_without_sha256_errors(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    _write_permissive_policy(tmp_path / "home")
    runner = CliRunner()
    result = runner.invoke(
        plugin_app,
        ["install", "https://example.com/x.tgz", "--id", "x"],
    )
    assert result.exit_code == 2, result.output
    assert "--sha256" in result.output


def test_verify_subcommand_prints_clean_report(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    _write_permissive_policy(tmp_path / "home")

    from opencomputer.plugins.installed_index import (
        InstalledRecord,
        record_install,
    )
    from opencomputer.plugins.integrity import DriftReport

    home = tmp_path / "home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    record_install(
        plugins_dir / ".installed_index.json",
        InstalledRecord(
            plugin_id="ok",
            version="0.1.0",
            source="catalog",
            source_url="ok",
            source_ref=None,
            tarball_sha256="abc",
            installed_at=0,
        ),
    )

    plugin_dir = plugins_dir / "ok"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"id":"ok","name":"ok","version":"0.1.0","entry":"p.py"}'
    )

    def fake_verify(plugin_id, *, dest_root, **kwargs):
        return DriftReport(
            plugin_id=plugin_id,
            source="catalog",
            source_url="ok",
            has_drift=False,
        )

    runner = CliRunner()
    with patch(
        "opencomputer.cli_plugin._verify_plugin", side_effect=fake_verify
    ):
        with patch(
            "opencomputer.cli_plugin._resolve_destination_root",
            return_value=plugins_dir,
        ):
            result = runner.invoke(plugin_app, ["verify", "ok"])

    assert result.exit_code == 0, result.output
    assert "no drift" in result.output.lower()


def test_verify_subcommand_unknown_plugin_errors(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    _write_permissive_policy(tmp_path / "home")
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()

    runner = CliRunner()
    with patch(
        "opencomputer.cli_plugin._resolve_destination_root",
        return_value=plugins_dir,
    ):
        result = runner.invoke(plugin_app, ["verify", "ghost"])
    assert result.exit_code == 2, result.output
    assert "not installed" in result.output.lower()
