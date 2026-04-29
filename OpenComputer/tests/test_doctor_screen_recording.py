"""Tests for the Screen Recording permission check in oc doctor (macOS only)."""
from __future__ import annotations

import sys
from unittest import mock


def test_check_function_exists():
    """The check function is importable."""
    from opencomputer.doctor import check_macos_screen_recording_permission

    assert callable(check_macos_screen_recording_permission)


def test_non_macos_returns_skipped():
    from opencomputer.doctor import check_macos_screen_recording_permission

    with mock.patch.object(sys, "platform", "linux"):
        result = check_macos_screen_recording_permission()
    assert result is None or "skipped" in str(result).lower()


def test_macos_permission_granted_returns_ok():
    """When the probe reports granted, the doctor result is ok."""
    if sys.platform != "darwin":
        return
    from opencomputer.doctor import check_macos_screen_recording_permission

    with mock.patch(
        "opencomputer.doctor._macos_screen_recording_granted", return_value=True
    ):
        result = check_macos_screen_recording_permission()
    s = str(result).lower()
    assert "ok" in s or "granted" in s


def test_macos_permission_missing_returns_warning():
    if sys.platform != "darwin":
        return
    from opencomputer.doctor import check_macos_screen_recording_permission

    with mock.patch(
        "opencomputer.doctor._macos_screen_recording_granted", return_value=False
    ):
        result = check_macos_screen_recording_permission()
    s = str(result).lower()
    assert "warn" in s or "missing" in s or "not granted" in s
