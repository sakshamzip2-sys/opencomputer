"""--install-daemon convenience flags on oc setup and oc gateway."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_gateway_install_daemon_calls_install_and_exits(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.install.return_value = MagicMock(
        backend="systemd", config_path="/tmp/x.service",
        enabled=True, started=True, notes=[],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["gateway", "--install-daemon"])
    assert result.exit_code == 0, result.stdout
    fake_backend.install.assert_called_once()
    # Should NOT have actually launched the gateway loop
    assert "Gateway connecting" not in result.stdout


def test_gateway_install_daemon_with_custom_profile(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.install.return_value = MagicMock(
        backend="launchd", config_path="/tmp/x.plist",
        enabled=True, started=True, notes=[],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(
            app, ["gateway", "--install-daemon", "--daemon-profile", "work"],
        )
    assert result.exit_code == 0
    call = fake_backend.install.call_args
    assert call.kwargs.get("profile") == "work"


def test_setup_install_daemon_runs_wizard_then_installs(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.install.return_value = MagicMock(
        backend="launchd", config_path="/tmp/x.plist",
        enabled=True, started=True, notes=[],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend), \
         patch("opencomputer.setup_wizard.run_setup") as legacy_wiz, \
         patch("opencomputer.cli_setup.wizard.run_setup") as new_wiz:
        legacy_wiz.return_value = None
        new_wiz.return_value = None
        result = runner.invoke(app, ["setup", "--install-daemon"])
    assert result.exit_code == 0, result.stdout
    assert legacy_wiz.called or new_wiz.called
    fake_backend.install.assert_called_once()
