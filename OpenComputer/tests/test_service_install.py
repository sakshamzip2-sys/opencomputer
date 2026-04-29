"""opencomputer service install writes a systemd-user unit; uninstall
removes it; status reports whether it's active."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_render_unit_substitutes_executable_and_workdir(tmp_path: Path) -> None:
    from opencomputer.service import render_systemd_unit

    out = render_systemd_unit(
        executable="/home/pi/.local/bin/opencomputer",
        workdir=tmp_path,
        profile="default",
        extra_args="gateway",
    )
    assert "[Unit]" in out
    assert "[Service]" in out
    assert "[Install]" in out
    assert "WantedBy=default.target" in out
    assert "ExecStart=/home/pi/.local/bin/opencomputer --headless --profile default gateway" in out
    assert f"WorkingDirectory={tmp_path}" in out
    # Restart-on-failure is the whole point — must be present.
    assert "Restart=always" in out


def test_install_writes_unit_to_user_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.service import install_systemd_unit

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.setattr("sys.platform", "linux")

    with patch("opencomputer.service._systemctl") as sysctl:
        sysctl.return_value = (0, "", "")
        path = install_systemd_unit(
            executable="/usr/local/bin/opencomputer",
            workdir=fake_home,
            profile="default",
            extra_args="gateway",
        )

    expected = fake_home / ".config" / "systemd" / "user" / "opencomputer.service"
    assert path == expected
    assert expected.exists()
    body = expected.read_text()
    assert "ExecStart=/usr/local/bin/opencomputer" in body
    assert sysctl.called  # daemon-reload was invoked


def test_uninstall_removes_unit_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.service import install_systemd_unit, uninstall_systemd_unit

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.setattr("sys.platform", "linux")

    with patch("opencomputer.service._systemctl") as sysctl:
        sysctl.return_value = (0, "", "")
        path = install_systemd_unit(
            executable="/usr/local/bin/opencomputer",
            workdir=fake_home,
            profile="default",
            extra_args="gateway",
        )
        assert path.exists()
        uninstall_systemd_unit()
        assert not path.exists()


def test_install_refuses_outside_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """systemd is Linux-only — install on Mac/Windows must raise loudly."""
    from opencomputer.service import ServiceUnsupportedError, install_systemd_unit

    monkeypatch.setattr("sys.platform", "darwin")
    with pytest.raises(ServiceUnsupportedError, match="systemd is Linux-only"):
        install_systemd_unit(
            executable="/x", workdir="/y", profile="p", extra_args=""
        )
