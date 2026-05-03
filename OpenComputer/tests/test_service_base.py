"""Protocol + result dataclasses for the cross-platform service backend."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_install_result_is_frozen_dataclass() -> None:
    from opencomputer.service.base import InstallResult

    r = InstallResult(
        backend="systemd",
        config_path=Path("/tmp/x.service"),
        enabled=True,
        started=True,
        notes=["hint"],
    )
    assert r.backend == "systemd"
    assert r.config_path == Path("/tmp/x.service")
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        r.backend = "launchd"  # type: ignore[misc]


def test_status_result_fields() -> None:
    from opencomputer.service.base import StatusResult

    s = StatusResult(
        backend="launchd",
        file_present=True,
        enabled=True,
        running=False,
        pid=None,
        uptime_seconds=None,
        last_log_lines=[],
    )
    assert s.backend == "launchd"
    assert s.pid is None


def test_uninstall_result_fields() -> None:
    from opencomputer.service.base import UninstallResult

    u = UninstallResult(
        backend="schtasks",
        file_removed=True,
        config_path=Path("/tmp/x.xml"),
        notes=[],
    )
    assert u.file_removed is True


def test_service_unsupported_error_is_runtime_error() -> None:
    from opencomputer.service.base import ServiceUnsupportedError

    assert issubclass(ServiceUnsupportedError, RuntimeError)
    err = ServiceUnsupportedError("no backend for platform foo")
    assert "no backend" in str(err)


def test_protocol_has_required_attrs() -> None:
    """ServiceBackend Protocol declares the expected methods + NAME class var."""
    from opencomputer.service.base import ServiceBackend

    expected_methods = {
        "supported", "install", "uninstall", "status",
        "start", "stop", "follow_logs",
    }
    actual = set(dir(ServiceBackend)) & expected_methods
    assert actual == expected_methods, f"missing methods: {expected_methods - actual}"
