"""B5: doctor advises `oc service start` when service enabled but not running."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from opencomputer.doctor import _check_service


def test_doctor_advises_service_start_when_enabled_not_running() -> None:
    """The 'enabled but not running' warning must include `oc service start` hint."""
    fake_status = MagicMock()
    fake_status.running = False
    fake_status.enabled = True
    fake_status.file_present = True
    fake_status.pid = None
    fake_backend = MagicMock()
    fake_backend.NAME = "launchd"
    fake_backend.status.return_value = fake_status

    with patch("opencomputer.service.factory.get_backend", return_value=fake_backend):
        result = _check_service()

    assert result.status == "warn"
    assert "oc service start" in result.detail
