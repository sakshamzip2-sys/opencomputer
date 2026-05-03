"""factory.get_backend dispatches on sys.platform."""
from __future__ import annotations

import pytest


def test_factory_returns_linux_backend_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    assert backend.NAME == "systemd"


def test_factory_returns_macos_backend_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    assert backend.NAME == "launchd"


def test_factory_returns_windows_backend_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    assert backend.NAME == "schtasks"


def test_factory_raises_on_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "freebsd14")
    from opencomputer.service.base import ServiceUnsupportedError
    from opencomputer.service.factory import get_backend

    with pytest.raises(ServiceUnsupportedError, match="freebsd14"):
        get_backend()


def test_factory_returned_backend_has_protocol_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    for name in ("supported", "install", "uninstall", "status",
                 "start", "stop", "follow_logs"):
        assert callable(getattr(backend, name)), f"backend missing {name}"
