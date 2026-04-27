"""Cross-platform foreground-detector tests (T2).

The macOS and Windows happy-path tests are guarded with ``pytest.mark.skipif``
because they require the host platform's tooling. Linux paths are fully
mocked (``shutil.which`` + ``subprocess.run``) so they run on any host. All
failure paths return None — the daemon treats None as "skip this tick".
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure the conftest alias machinery has been imported.
pytest.importorskip("extensions.coding_harness")

from extensions.ambient_sensors.foreground import (  # noqa: E402
    ForegroundSnapshot,
    _detect_linux,
    _detect_macos,
    _detect_windows,
    detect_foreground,
)


def test_snapshot_is_frozen_dataclass() -> None:
    """ForegroundSnapshot must be immutable so callers can hash/cache it."""
    import dataclasses

    snap = ForegroundSnapshot(
        app_name="Code",
        window_title="t",
        bundle_id="b",
        platform="darwin",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.app_name = "x"  # type: ignore[misc]


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only path")
def test_macos_calls_osascript() -> None:
    fake_run = MagicMock(
        return_value=MagicMock(
            stdout="Code\nfile.py — Code\ncom.microsoft.VSCode\n",
            returncode=0,
        )
    )
    with patch("extensions.ambient_sensors.foreground.subprocess.run", fake_run):
        snap = _detect_macos()
    assert snap is not None
    assert snap.app_name == "Code"
    assert "file.py" in snap.window_title
    assert snap.bundle_id == "com.microsoft.VSCode"
    assert snap.platform == "darwin"


def test_macos_returns_none_on_failure() -> None:
    """If osascript is missing/broken, return None (daemon will skip)."""
    fake_run = MagicMock(side_effect=FileNotFoundError("osascript missing"))
    with patch("extensions.ambient_sensors.foreground.subprocess.run", fake_run):
        snap = _detect_macos()
    assert snap is None


def test_macos_returns_none_on_nonzero_exit() -> None:
    """osascript can return nonzero (e.g. user denied automation perms)."""
    fake_run = MagicMock(return_value=MagicMock(stdout="", returncode=1, stderr="denied"))
    with patch("extensions.ambient_sensors.foreground.subprocess.run", fake_run):
        snap = _detect_macos()
    assert snap is None


def test_macos_returns_none_on_timeout() -> None:
    """osascript can hang on slow systems; we hard-cap at 2s."""
    import subprocess as _sp

    fake_run = MagicMock(side_effect=_sp.TimeoutExpired(cmd="osascript", timeout=2.0))
    with patch("extensions.ambient_sensors.foreground.subprocess.run", fake_run):
        snap = _detect_macos()
    assert snap is None


def test_linux_returns_none_on_wayland() -> None:
    """Wayland-only sessions (WAYLAND_DISPLAY set, DISPLAY empty) return None."""
    fake_environ = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ""}
    with patch.dict("os.environ", fake_environ, clear=True):
        snap = _detect_linux()
    assert snap is None


def test_linux_uses_xdotool_when_available() -> None:
    fake_which = MagicMock(side_effect=lambda c: "/usr/bin/xdotool" if c == "xdotool" else None)
    fake_run = MagicMock(
        side_effect=[
            MagicMock(stdout="123\n", returncode=0),  # getactivewindow
            MagicMock(stdout="my-file.py - VS Code\n", returncode=0),  # getwindowname
            MagicMock(stdout="code.Code\n", returncode=0),  # getwindowclassname
        ]
    )
    with (
        patch("extensions.ambient_sensors.foreground.shutil.which", fake_which),
        patch("extensions.ambient_sensors.foreground.subprocess.run", fake_run),
        patch.dict("os.environ", {"DISPLAY": ":0", "WAYLAND_DISPLAY": ""}, clear=True),
    ):
        snap = _detect_linux()
    assert snap is not None
    assert "Code" in snap.app_name
    assert "my-file.py" in snap.window_title
    assert snap.platform == "linux"


def test_linux_falls_back_to_wmctrl() -> None:
    """When xdotool absent but wmctrl present, parse `wmctrl -l` output."""
    fake_which = MagicMock(side_effect=lambda c: "/usr/bin/wmctrl" if c == "wmctrl" else None)
    fake_run = MagicMock(
        return_value=MagicMock(
            stdout="0x0123 0 host my-file.py - VS Code\n",
            returncode=0,
        )
    )
    with (
        patch("extensions.ambient_sensors.foreground.shutil.which", fake_which),
        patch("extensions.ambient_sensors.foreground.subprocess.run", fake_run),
        patch.dict("os.environ", {"DISPLAY": ":0", "WAYLAND_DISPLAY": ""}, clear=True),
    ):
        snap = _detect_linux()
    assert snap is not None
    assert "my-file.py" in snap.window_title
    assert snap.platform == "linux"


def test_linux_returns_none_when_no_tools_available() -> None:
    """Neither xdotool nor wmctrl on PATH → None (daemon will skip)."""
    fake_which = MagicMock(return_value=None)
    with (
        patch("extensions.ambient_sensors.foreground.shutil.which", fake_which),
        patch.dict("os.environ", {"DISPLAY": ":0", "WAYLAND_DISPLAY": ""}, clear=True),
    ):
        snap = _detect_linux()
    assert snap is None


@pytest.mark.skipif(sys.platform != "win32", reason="windows-only path")
def test_windows_uses_win32gui() -> None:
    pytest.importorskip("win32gui")
    # Smoke-test: actual call may legitimately return None on CI VM with no foreground window.
    snap = _detect_windows()
    assert snap is None or snap.platform == "win32"


def test_windows_returns_none_when_pywin32_missing() -> None:
    """On hosts without pywin32 the import fails and we return None."""
    if sys.platform == "win32":
        pytest.skip("real pywin32 is installed on this host")
    snap = _detect_windows()
    assert snap is None


def test_detect_foreground_dispatches_by_platform() -> None:
    """The top-level detect_foreground() must call the right platform helper."""
    if sys.platform == "darwin":
        with patch(
            "extensions.ambient_sensors.foreground._detect_macos",
            return_value=None,
        ) as m:
            detect_foreground()
            m.assert_called_once()
    elif sys.platform.startswith("linux"):
        with patch(
            "extensions.ambient_sensors.foreground._detect_linux",
            return_value=None,
        ) as m:
            detect_foreground()
            m.assert_called_once()
    elif sys.platform == "win32":
        with patch(
            "extensions.ambient_sensors.foreground._detect_windows",
            return_value=None,
        ) as m:
            detect_foreground()
            m.assert_called_once()
    else:
        # Unknown platform → returns None directly.
        assert detect_foreground() is None
