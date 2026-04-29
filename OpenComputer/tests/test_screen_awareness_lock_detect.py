"""Tests for is_screen_locked() — cross-platform skip semantics with
mocked OS calls. The contract: any uncertainty maps to LOCKED (fail-safe;
no capture)."""
from __future__ import annotations

from unittest import mock


def test_macos_unlocked_returns_false():
    """When _macos_locked reports False, is_screen_locked returns False."""
    from extensions.screen_awareness import lock_detect

    with mock.patch.object(lock_detect, "sys") as fake_sys, \
         mock.patch.object(lock_detect, "_macos_locked", return_value=False):
        fake_sys.platform = "darwin"
        assert lock_detect.is_screen_locked() is False


def test_macos_locked_returns_true():
    from extensions.screen_awareness import lock_detect

    with mock.patch.object(lock_detect, "sys") as fake_sys, \
         mock.patch.object(lock_detect, "_macos_locked", return_value=True):
        fake_sys.platform = "darwin"
        assert lock_detect.is_screen_locked() is True


def test_unknown_platform_fail_safe_returns_true():
    """An unrecognized sys.platform returns True (locked) — fail-safe.
    No capture is the right default if we can't tell."""
    from extensions.screen_awareness import lock_detect

    with mock.patch.object(lock_detect, "sys") as fake_sys:
        fake_sys.platform = "haiku"
        assert lock_detect.is_screen_locked() is True


def test_macos_quartz_import_fail_returns_true():
    """If Quartz import fails on macOS, fail-safe to True (locked)."""
    from extensions.screen_awareness.lock_detect import _macos_locked

    # Force Quartz import to fail by patching sys.modules.
    with mock.patch.dict("sys.modules", {"Quartz": None}):
        assert _macos_locked() is True


def test_linux_xdg_screensaver_active_returns_true():
    """xdg-screensaver status outputs 'active' when locked."""
    from extensions.screen_awareness.lock_detect import _linux_locked

    with mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(stdout="active\n", returncode=0)
        assert _linux_locked() is True


def test_linux_xdg_screensaver_inactive_returns_false():
    from extensions.screen_awareness.lock_detect import _linux_locked

    with mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(stdout="inactive\n", returncode=0)
        assert _linux_locked() is False


def test_linux_xdg_screensaver_missing_returns_true():
    """If xdg-screensaver is not installed, fail-safe to True."""
    from extensions.screen_awareness.lock_detect import _linux_locked

    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        assert _linux_locked() is True
