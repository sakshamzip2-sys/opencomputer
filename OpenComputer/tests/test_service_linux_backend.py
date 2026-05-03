"""Linux systemd-user backend (gateway, not the daily profile-analyze cron)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_install_writes_unit_with_restart_always(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _linux_systemd

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(
        _linux_systemd, "_resolve_executable",
        lambda: "/usr/local/bin/oc",
    )

    with patch.object(_linux_systemd, "_systemctl") as sysctl:
        sysctl.return_value = (0, "active", "")
        result = _linux_systemd.install(profile="default", extra_args="gateway")

    expected = fake_home / ".config" / "systemd" / "user" / "opencomputer.service"
    assert result.config_path == expected
    assert result.backend == "systemd"
    body = expected.read_text()
    assert "Restart=always" in body
    assert "ExecStart=/usr/local/bin/oc --headless --profile default gateway" in body


def test_uninstall_removes_unit_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _linux_systemd

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(
        _linux_systemd, "_resolve_executable",
        lambda: "/usr/local/bin/oc",
    )

    with patch.object(_linux_systemd, "_systemctl") as sysctl:
        sysctl.return_value = (0, "", "")
        result_install = _linux_systemd.install(profile="default", extra_args="gateway")
        assert result_install.config_path.exists()
        result_uninstall = _linux_systemd.uninstall()
        assert not result_install.config_path.exists()
        assert result_uninstall.file_removed is True


def test_supported_returns_true_only_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _linux_systemd

    monkeypatch.setattr("sys.platform", "linux")
    assert _linux_systemd.supported() is True
    monkeypatch.setattr("sys.platform", "darwin")
    assert _linux_systemd.supported() is False


def test_status_reports_running_active_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _linux_systemd

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    unit_path = _linux_systemd._user_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text("(unit body)")

    def fake_systemctl(*args):
        if args == ("is-enabled", "opencomputer.service"):
            return (0, "enabled\n", "")
        if args == ("is-active", "opencomputer.service"):
            return (0, "active\n", "")
        if args[0] == "show":
            return (
                0,
                "MainPID=12345\nActiveEnterTimestampMonotonic=42000000\n",
                "",
            )
        return (0, "", "")

    monkeypatch.setattr(_linux_systemd, "_systemctl", fake_systemctl)
    monkeypatch.setattr(
        _linux_systemd, "_journalctl_tail",
        lambda n: ["log line A", "log line B"],
    )

    s = _linux_systemd.status()
    assert s.backend == "systemd"
    assert s.file_present is True
    assert s.enabled is True
    assert s.running is True
    assert s.pid == 12345
    assert s.last_log_lines == ["log line A", "log line B"]


def test_status_reports_missing_file_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _linux_systemd

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(_linux_systemd, "_systemctl", lambda *a: (3, "", ""))
    monkeypatch.setattr(_linux_systemd, "_journalctl_tail", lambda n: [])

    s = _linux_systemd.status()
    assert s.file_present is False
    assert s.enabled is False
    assert s.running is False
    assert s.pid is None


def test_start_invokes_systemctl_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _linux_systemd

    calls: list = []
    monkeypatch.setattr(
        _linux_systemd, "_systemctl",
        lambda *a: (calls.append(a) or (0, "", "")),
    )
    assert _linux_systemd.start() is True
    assert ("start", "opencomputer.service") in calls


def test_stop_invokes_systemctl_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _linux_systemd

    calls: list = []
    monkeypatch.setattr(
        _linux_systemd, "_systemctl",
        lambda *a: (calls.append(a) or (0, "", "")),
    )
    assert _linux_systemd.stop() is True
    assert ("stop", "opencomputer.service") in calls


def test_follow_logs_returns_journalctl_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _linux_systemd

    monkeypatch.setattr(
        _linux_systemd, "_journalctl_tail",
        lambda n: ["a", "b", "c"],
    )
    out = list(_linux_systemd.follow_logs(lines=3, follow=False))
    assert out == ["a", "b", "c"]
