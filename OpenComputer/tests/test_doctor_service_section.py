"""oc doctor includes a service health row that wraps factory.get_backend()."""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_check_service_reports_running() -> None:
    from opencomputer.doctor import _check_service

    fake_backend = MagicMock()
    fake_backend.NAME = "systemd"
    fake_backend.status.return_value = MagicMock(
        backend="systemd", file_present=True, enabled=True, running=True,
        pid=12345, uptime_seconds=None, last_log_lines=[],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        check = _check_service()
    assert check.status == "pass"
    assert "running" in check.detail


def test_check_service_reports_warn_when_enabled_not_running() -> None:
    from opencomputer.doctor import _check_service

    fake_backend = MagicMock()
    fake_backend.NAME = "launchd"
    fake_backend.status.return_value = MagicMock(
        backend="launchd", file_present=True, enabled=True, running=False,
        pid=None, uptime_seconds=None, last_log_lines=[],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        check = _check_service()
    assert check.status == "warn"


def test_check_service_handles_unsupported_platform() -> None:
    from opencomputer.doctor import _check_service
    from opencomputer.service.base import ServiceUnsupportedError

    with patch(
        "opencomputer.service.factory.get_backend",
        side_effect=ServiceUnsupportedError("no backend for platform foo"),
    ):
        check = _check_service()
    assert check.status == "skip"
    assert "not supported" in check.detail.lower()


def test_check_service_skips_when_not_installed() -> None:
    from opencomputer.doctor import _check_service

    fake_backend = MagicMock()
    fake_backend.NAME = "schtasks"
    fake_backend.status.return_value = MagicMock(
        backend="schtasks", file_present=False, enabled=False, running=False,
        pid=None, uptime_seconds=None, last_log_lines=[],
    )
    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        check = _check_service()
    assert check.status == "skip"
    assert "not installed" in check.detail.lower()
