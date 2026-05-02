"""OS-specific Chrome launch command helper."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_chrome_launch_module():
    repo = Path(__file__).resolve().parent.parent
    path = repo / "extensions" / "browser-control" / "chrome_launch.py"
    module_name = f"_chrome_launch_under_test_{id(path)}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_macos_command_has_chrome_and_debug_port():
    mod = _load_chrome_launch_module()
    cmd = mod.chrome_launch_command(platform="darwin")
    # macOS path has shell-escaped space ("Google\\ Chrome").
    assert "Google" in cmd
    assert "Chrome" in cmd
    assert "/Applications/" in cmd
    assert "--remote-debugging-port=9222" in cmd


def test_linux_command_has_google_chrome_and_debug_port():
    mod = _load_chrome_launch_module()
    cmd = mod.chrome_launch_command(platform="linux")
    assert "google-chrome" in cmd
    assert "--remote-debugging-port=9222" in cmd


def test_windows_command_has_chrome_exe_and_debug_port():
    mod = _load_chrome_launch_module()
    cmd = mod.chrome_launch_command(platform="win32")
    assert "chrome.exe" in cmd
    assert "--remote-debugging-port=9222" in cmd


def test_unknown_platform_raises_with_helpful_message():
    mod = _load_chrome_launch_module()
    with pytest.raises(NotImplementedError) as exc_info:
        mod.chrome_launch_command(platform="freebsd")
    msg = str(exc_info.value)
    assert "--remote-debugging-port=9222" in msg
    assert "OPENCOMPUTER_BROWSER_CDP_URL" in msg


def test_default_platform_is_sys_platform():
    """Calling with no argument uses sys.platform."""
    mod = _load_chrome_launch_module()
    if sys.platform in mod.CHROME_LAUNCH_COMMANDS:
        # Should return without raising.
        cmd = mod.chrome_launch_command()
        assert "--remote-debugging-port=9222" in cmd
