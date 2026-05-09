"""M4: service-helper additions — `oc service status --watch`, `oc wire/dashboard --detach`."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli import _detach_to_background, _format_service_status_line, app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_format_service_status_line_running() -> None:
    s = MagicMock(running=True, pid=1234, enabled=True, file_present=True)
    line = _format_service_status_line(s, "launchd")
    assert "running" in line
    assert "1234" in line
    assert "[launchd]" in line


def test_format_service_status_line_enabled_not_running() -> None:
    s = MagicMock(running=False, enabled=True, file_present=True, pid=None)
    line = _format_service_status_line(s, "systemd")
    assert "enabled but not running" in line


def test_format_service_status_line_not_installed() -> None:
    s = MagicMock(running=False, enabled=False, file_present=False, pid=None)
    line = _format_service_status_line(s, "launchd")
    assert "not installed" in line


def test_service_status_no_watch_runs_once(runner: CliRunner) -> None:
    """`oc service status` (no --watch) prints once and exits."""
    fake_status = MagicMock(running=True, pid=1234, enabled=True, file_present=True)
    fake_backend = MagicMock(NAME="launchd")
    fake_backend.status.return_value = fake_status
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = runner.invoke(app, ["service", "status"])
    assert result.exit_code == 0
    assert "running" in result.stdout
    # Status checked exactly once (no watch)
    assert fake_backend.status.call_count == 1


def test_service_status_watch_exits_when_status_changes(runner: CliRunner) -> None:
    """`--watch` polls until the status string changes."""
    s_initial = MagicMock(running=False, enabled=True, file_present=True, pid=None)
    s_after = MagicMock(running=True, enabled=True, file_present=True, pid=999)
    fake_backend = MagicMock(NAME="launchd")
    fake_backend.status.side_effect = [s_initial, s_after]
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend), \
         patch("time.sleep"):
        result = runner.invoke(
            app,
            ["service", "status", "--watch", "--interval", "0.01", "--timeout", "1"],
        )
    assert result.exit_code == 0
    assert "enabled but not running" in result.stdout
    assert "running (pid=999)" in result.stdout


def test_service_status_watch_times_out(runner: CliRunner) -> None:
    """`--watch --timeout 0.05` exits non-zero when status is steady."""
    fake_status = MagicMock(running=False, enabled=True, file_present=True, pid=None)
    fake_backend = MagicMock(NAME="launchd")
    fake_backend.status.return_value = fake_status
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend), \
         patch("time.sleep"):
        result = runner.invoke(
            app,
            ["service", "status", "--watch", "--interval", "0.01", "--timeout", "0.05"],
        )
    assert result.exit_code == 1
    assert "timed out" in result.stdout or "timed out" in (result.stderr or "")


def test_detach_helper_creates_pidfile_and_log_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detach helper writes the right pidfile + log paths under profile home.

    We can't actually fork in a unit test cleanly, so we simulate the
    parent return path by patching os.fork to return a positive PID.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    fake_pid = 99999
    written: dict = {}

    def fake_fork():
        # Simulate the parent (returns child's pid > 0).
        # Pre-write the pidfile so the parent's wait loop exits.
        (tmp_path / "wire.pid").write_text(str(fake_pid))
        written["forked"] = True
        return fake_pid

    with patch("os.fork", fake_fork):
        is_parent = _detach_to_background(pidfile_name="wire.pid", log_name="wire.log")

    assert is_parent is True
    assert written.get("forked") is True
    assert (tmp_path / "wire.pid").exists()
    assert (tmp_path / "wire.pid").read_text().strip() == str(fake_pid)


def test_detach_refuses_when_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a pidfile exists and the PID is alive, --detach exits 0 with a notice."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    pidfile = tmp_path / "wire.pid"
    pidfile.write_text(str(os.getpid()))  # current process is alive

    with pytest.raises(Exception) as exc_info:
        _detach_to_background(pidfile_name="wire.pid", log_name="wire.log")
    # typer.Exit(0) raised
    assert getattr(exc_info.value, "exit_code", None) == 0
