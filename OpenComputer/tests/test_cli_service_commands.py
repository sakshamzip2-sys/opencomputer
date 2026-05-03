"""New oc service subcommands: start, stop, logs, doctor."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_service_start_invokes_factory_backend_start(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.start.return_value = True
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["service", "start"])
    assert result.exit_code == 0
    fake_backend.start.assert_called_once()


def test_service_stop_invokes_factory_backend_stop(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.stop.return_value = True
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["service", "stop"])
    assert result.exit_code == 0
    fake_backend.stop.assert_called_once()


def test_service_logs_returns_recent_lines(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_backend.follow_logs.return_value = iter(["line a", "line b"])
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["service", "logs", "-n", "2"])
    assert result.exit_code == 0
    assert "line a" in result.stdout
    assert "line b" in result.stdout


def test_service_doctor_reports_health_checks(runner: CliRunner) -> None:
    from opencomputer.cli import app

    fake_backend = MagicMock()
    fake_status = MagicMock(
        backend="systemd", file_present=True, enabled=True, running=True,
        pid=12345, uptime_seconds=None, last_log_lines=["ok"],
    )
    fake_backend.status.return_value = fake_status
    fake_backend.NAME = "systemd"
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["service", "doctor"])
    assert result.exit_code == 0
    assert "config_file_present" in result.stdout
    assert "service_running" in result.stdout
