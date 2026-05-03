"""Regression tests: oc service install/uninstall/status work on every platform.

The bug: PR #378 added cross-platform start/stop/logs/doctor but left the
existing install/uninstall/status calling the Linux-only legacy shims
(install_systemd_unit / uninstall_systemd_unit / is_active). On macOS,
`oc service install` raised ServiceUnsupportedError; `oc service status`
silently lied with "inactive".

These tests freeze the contract: every legacy CLI command must route
through factory.get_backend() and work on all three platforms.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _fake_backend(name: str = "launchd") -> MagicMock:
    backend = MagicMock()
    backend.NAME = name
    backend.install.return_value = MagicMock(
        backend=name,
        config_path=Path(f"/tmp/x.{name}"),
        enabled=True,
        started=True,
        notes=[],
    )
    backend.uninstall.return_value = MagicMock(
        backend=name,
        file_removed=True,
        config_path=Path(f"/tmp/x.{name}"),
        notes=[],
    )
    backend.status.return_value = MagicMock(
        backend=name,
        file_present=True,
        enabled=True,
        running=True,
        pid=12345,
        uptime_seconds=None,
        last_log_lines=[],
    )
    return backend


# ─── install ──────────────────────────────────────────────────────────


def test_service_install_routes_through_factory_on_macos(runner: CliRunner) -> None:
    """The bug: this raised ServiceUnsupportedError on darwin."""
    from opencomputer.cli import app

    backend = _fake_backend("launchd")
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(app, ["service", "install"])
    assert result.exit_code == 0, result.stdout
    backend.install.assert_called_once()
    assert "installed (launchd)" in result.stdout


def test_service_install_routes_through_factory_on_linux(runner: CliRunner) -> None:
    from opencomputer.cli import app

    backend = _fake_backend("systemd")
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(app, ["service", "install"])
    assert result.exit_code == 0
    backend.install.assert_called_once()
    assert "installed (systemd)" in result.stdout


def test_service_install_routes_through_factory_on_windows(runner: CliRunner) -> None:
    from opencomputer.cli import app

    backend = _fake_backend("schtasks")
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(app, ["service", "install"])
    assert result.exit_code == 0
    backend.install.assert_called_once()
    assert "installed (schtasks)" in result.stdout


def test_service_install_passes_profile_and_extra_args(runner: CliRunner) -> None:
    """The legacy --profile and --extra-args options stay backward compatible."""
    from opencomputer.cli import app

    backend = _fake_backend("launchd")
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(
            app, ["service", "install", "--profile", "work", "--extra-args", "gateway"],
        )
    assert result.exit_code == 0
    backend.install.assert_called_once_with(profile="work", extra_args="gateway")


def test_service_install_surfaces_notes(runner: CliRunner) -> None:
    """Backend hints (e.g. enable-linger reminder) reach the user."""
    from opencomputer.cli import app

    backend = _fake_backend("systemd")
    backend.install.return_value = MagicMock(
        backend="systemd",
        config_path=Path("/tmp/x.service"),
        enabled=True,
        started=True,
        notes=[
            "On a headless Linux server, run `sudo loginctl enable-linger $USER` …",
        ],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(app, ["service", "install"])
    assert result.exit_code == 0
    assert "enable-linger" in result.stdout


def test_service_install_when_enable_call_fails(runner: CliRunner) -> None:
    """File written, OS register failed → warning visible, non-fatal."""
    from opencomputer.cli import app

    backend = _fake_backend("launchd")
    backend.install.return_value = MagicMock(
        backend="launchd",
        config_path=Path("/tmp/x.plist"),
        enabled=False,
        started=False,
        notes=["bootstrap returned 5; run manually with launchctl bootstrap …"],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(app, ["service", "install"])
    assert result.exit_code == 0
    assert "warning: file written" in result.stdout


# ─── uninstall ────────────────────────────────────────────────────────


def test_service_uninstall_routes_through_factory_on_macos(runner: CliRunner) -> None:
    from opencomputer.cli import app

    backend = _fake_backend("launchd")
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(app, ["service", "uninstall"])
    assert result.exit_code == 0
    backend.uninstall.assert_called_once()
    assert "removed (launchd)" in result.stdout


def test_service_uninstall_when_nothing_installed(runner: CliRunner) -> None:
    from opencomputer.cli import app

    backend = _fake_backend("schtasks")
    backend.uninstall.return_value = MagicMock(
        backend="schtasks",
        file_removed=False,
        config_path=None,
        notes=[],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(app, ["service", "uninstall"])
    assert result.exit_code == 0
    assert "no service installed (schtasks backend)" in result.stdout


# ─── status ───────────────────────────────────────────────────────────


def test_service_status_running(runner: CliRunner) -> None:
    """The other half of the bug: status used to silently say 'inactive' on macOS."""
    from opencomputer.cli import app

    backend = _fake_backend("launchd")
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(app, ["service", "status"])
    assert result.exit_code == 0
    assert "running" in result.stdout
    assert "[launchd]" in result.stdout
    assert "pid=12345" in result.stdout


def test_service_status_enabled_not_running(runner: CliRunner) -> None:
    from opencomputer.cli import app

    backend = _fake_backend("systemd")
    backend.status.return_value = MagicMock(
        backend="systemd", file_present=True, enabled=True, running=False,
        pid=None, uptime_seconds=None, last_log_lines=[],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(app, ["service", "status"])
    assert result.exit_code == 0
    assert "enabled but not running [systemd]" in result.stdout


def test_service_status_installed_but_not_enabled(runner: CliRunner) -> None:
    from opencomputer.cli import app

    backend = _fake_backend("launchd")
    backend.status.return_value = MagicMock(
        backend="launchd", file_present=True, enabled=False, running=False,
        pid=None, uptime_seconds=None, last_log_lines=[],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(app, ["service", "status"])
    assert result.exit_code == 0
    assert "installed but not enabled [launchd]" in result.stdout


def test_service_status_not_installed(runner: CliRunner) -> None:
    from opencomputer.cli import app

    backend = _fake_backend("schtasks")
    backend.status.return_value = MagicMock(
        backend="schtasks", file_present=False, enabled=False, running=False,
        pid=None, uptime_seconds=None, last_log_lines=[],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=backend):
        result = runner.invoke(app, ["service", "status"])
    assert result.exit_code == 0
    assert "not installed [schtasks]" in result.stdout
