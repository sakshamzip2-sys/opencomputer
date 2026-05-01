"""Plan 3 Task 4 — launchd plist install/uninstall tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from opencomputer.service.launchd import (
    LaunchdUnsupportedError,
    install_launchd_plist,
    render_launchd_plist,
    uninstall_launchd_plist,
)


def test_render_launchd_plist_contains_required_keys() -> None:
    """Rendered plist must have Label, ProgramArguments, StartCalendarInterval."""
    body = render_launchd_plist(
        executable="/usr/local/bin/opencomputer",
        hour=9,
    )
    assert "<key>Label</key>" in body
    assert "com.opencomputer.profile-analyze" in body
    assert "<key>ProgramArguments</key>" in body
    assert "<key>StartCalendarInterval</key>" in body
    assert "<integer>9</integer>" in body
    # Args must include the analyze subcommand
    assert "<string>profile</string>" in body
    assert "<string>analyze</string>" in body
    assert "<string>run</string>" in body


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
def test_install_writes_plist_to_launchagents(tmp_path: Path, monkeypatch) -> None:
    """install_launchd_plist writes to a directory monkeypatched to tmp_path."""
    monkeypatch.setattr(
        "opencomputer.service.launchd._launch_agents_dir",
        lambda: tmp_path,
    )
    path = install_launchd_plist(executable="/bin/echo", hour=9)
    assert path.exists()
    assert path.name == "com.opencomputer.profile-analyze.plist"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
def test_uninstall_removes_plist(tmp_path: Path, monkeypatch) -> None:
    """uninstall_launchd_plist removes the file if present."""
    monkeypatch.setattr(
        "opencomputer.service.launchd._launch_agents_dir",
        lambda: tmp_path,
    )
    install_launchd_plist(executable="/bin/echo", hour=9)
    plist_path = tmp_path / "com.opencomputer.profile-analyze.plist"
    assert plist_path.exists()
    removed = uninstall_launchd_plist()
    assert removed == plist_path
    assert not plist_path.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
def test_uninstall_when_absent_returns_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "opencomputer.service.launchd._launch_agents_dir",
        lambda: tmp_path,
    )
    assert uninstall_launchd_plist() is None


def test_install_rejects_non_macos(monkeypatch) -> None:
    """LaunchdUnsupportedError raised on non-macOS."""
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(LaunchdUnsupportedError):
        install_launchd_plist(executable="/bin/echo", hour=9)
