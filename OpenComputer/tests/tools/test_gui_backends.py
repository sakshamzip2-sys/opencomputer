"""Tests for the shared GUI-backend dispatch helpers."""
from unittest.mock import patch

from opencomputer.tools._gui_backends import (
    detect_linux_display_server,
    detect_platform,
    has_command,
    has_pyautogui,
)


def test_detect_platform_macos():
    with patch("opencomputer.tools._gui_backends.sys.platform", "darwin"):
        assert detect_platform() == "macos"


def test_detect_platform_linux():
    with patch("opencomputer.tools._gui_backends.sys.platform", "linux"):
        assert detect_platform() == "linux"


def test_detect_platform_linux_variant():
    with patch("opencomputer.tools._gui_backends.sys.platform", "linux2"):
        assert detect_platform() == "linux"


def test_detect_platform_windows():
    with patch("opencomputer.tools._gui_backends.sys.platform", "win32"):
        assert detect_platform() == "windows"


def test_detect_platform_cygwin():
    with patch("opencomputer.tools._gui_backends.sys.platform", "cygwin"):
        assert detect_platform() == "windows"


def test_detect_platform_unknown():
    with patch("opencomputer.tools._gui_backends.sys.platform", "freebsd"):
        assert detect_platform() == "unknown"


def test_detect_linux_x11(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert detect_linux_display_server() == "x11"


def test_detect_linux_wayland(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert detect_linux_display_server() == "wayland"


def test_detect_linux_default_when_unset(monkeypatch):
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    assert detect_linux_display_server() == "x11"


def test_detect_linux_unknown_value_defaults_x11(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "tty")
    assert detect_linux_display_server() == "x11"


def test_has_command_present():
    assert has_command("python3") or has_command("python")


def test_has_command_missing():
    assert not has_command("definitely-not-a-real-binary-xyz123")


def test_has_pyautogui_consistent_with_find_spec():
    import importlib.util

    expected = importlib.util.find_spec("pyautogui") is not None
    assert has_pyautogui() == expected
