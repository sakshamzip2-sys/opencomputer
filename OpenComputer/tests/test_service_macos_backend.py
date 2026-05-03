"""macOS launchd gateway backend (NOT the daily profile-analyze cron)."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_render_plist_substitutes_fields(tmp_path: Path) -> None:
    from opencomputer.service import _macos_launchd

    body = _macos_launchd._render_plist(
        executable="/opt/homebrew/bin/oc",
        workdir=tmp_path,
        profile="default",
        stdout_log=tmp_path / "stdout.log",
        stderr_log=tmp_path / "stderr.log",
    )
    assert "<string>/opt/homebrew/bin/oc</string>" in body
    assert "<string>--profile</string>" in body
    assert "<string>default</string>" in body
    assert "<key>KeepAlive</key>" in body
    assert "<true/>" in body
    # Plist is well-formed XML
    import xml.etree.ElementTree as ET
    ET.fromstring(body)
    # argv ends at "gateway" (no "run")
    assert "<string>run</string>" not in body


def test_install_writes_plist_and_calls_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _macos_launchd

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(_macos_launchd, "_resolve_executable", lambda: "/usr/local/bin/oc")
    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)

    calls: list[tuple[str, ...]] = []

    def fake_launchctl(*a):
        calls.append(a)
        return (0, "", "")

    monkeypatch.setattr(_macos_launchd, "_launchctl", fake_launchctl)

    result = _macos_launchd.install(profile="default", extra_args="")
    expected = fake_home / "Library" / "LaunchAgents" / "com.opencomputer.gateway.plist"
    assert result.config_path == expected
    assert expected.exists()
    body = expected.read_text()
    assert "com.opencomputer.gateway" in body
    bootstrap_calls = [c for c in calls if c[:1] == ("bootstrap",)]
    assert bootstrap_calls
    assert bootstrap_calls[0][1] == "gui/501"


def test_uninstall_removes_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _macos_launchd

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(_macos_launchd, "_resolve_executable", lambda: "/usr/local/bin/oc")
    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)
    monkeypatch.setattr(_macos_launchd, "_launchctl", lambda *a: (0, "", ""))

    install_result = _macos_launchd.install(profile="default", extra_args="")
    assert install_result.config_path.exists()

    uninstall_result = _macos_launchd.uninstall()
    assert uninstall_result.file_removed is True
    assert not install_result.config_path.exists()


def test_supported_returns_true_only_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr("sys.platform", "darwin")
    assert _macos_launchd.supported() is True
    monkeypatch.setattr("sys.platform", "linux")
    assert _macos_launchd.supported() is False


def test_status_parses_launchctl_print(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)

    fake_plist = tmp_path / "com.opencomputer.gateway.plist"
    fake_plist.write_text("(stub)")
    monkeypatch.setattr(_macos_launchd, "_plist_path", lambda: fake_plist)

    sample_print = """\
gui/501/com.opencomputer.gateway = {
\tactive count = 1
\tpath = /Users/me/Library/LaunchAgents/com.opencomputer.gateway.plist
\ttype = LaunchAgent
\tstate = running
\tpid = 91234
}"""

    def fake_launchctl(*args):
        if args[0] == "print":
            return (0, sample_print, "")
        return (0, "", "")

    monkeypatch.setattr(_macos_launchd, "_launchctl", fake_launchctl)
    monkeypatch.setattr(
        "opencomputer.service._common.tail_lines",
        lambda p, n: ["log a", "log b"],
    )

    s = _macos_launchd.status()
    assert s.backend == "launchd"
    assert s.file_present is True
    assert s.running is True
    assert s.pid == 91234


def test_start_kickstart(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)
    calls: list = []
    monkeypatch.setattr(
        _macos_launchd, "_launchctl",
        lambda *a: (calls.append(a) or (0, "", "")),
    )
    assert _macos_launchd.start() is True
    assert any(c[0] == "kickstart" for c in calls)


def test_stop_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setattr(_macos_launchd, "_uid", lambda: 501)
    calls: list = []
    monkeypatch.setattr(
        _macos_launchd, "_launchctl",
        lambda *a: (calls.append(a) or (0, "", "")),
    )
    assert _macos_launchd.stop() is True
    assert any(c[0] == "kill" for c in calls)


def test_follow_logs_tails_stdout_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.service import _macos_launchd

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    log_dir = tmp_path / ".opencomputer" / "default" / "logs"
    log_dir.mkdir(parents=True)
    out_log = log_dir / "gateway.stdout.log"
    out_log.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")

    out = list(_macos_launchd.follow_logs(lines=3, follow=False))
    assert out[-3:] == ["line 7", "line 8", "line 9"]
