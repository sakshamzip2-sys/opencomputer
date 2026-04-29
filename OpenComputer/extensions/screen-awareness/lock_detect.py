"""Cross-platform is_screen_locked() — fail-safe (any uncertainty → True).

When the screen is locked, asleep, or we can't tell, return True so the
sensor skips capture. Capturing a locked screen yields a black image (mss)
or a permission error — not useful, possibly leaks the lock-screen UI's
"User name" hint. Skip is the right default.
"""
from __future__ import annotations

import logging
import subprocess
import sys

_log = logging.getLogger("opencomputer.screen_awareness.lock_detect")


def is_screen_locked() -> bool:
    """Return True if the screen is locked, asleep, or undetectable."""
    if sys.platform == "darwin":
        return _macos_locked()
    if sys.platform.startswith("linux"):
        return _linux_locked()
    if sys.platform == "win32":
        return _windows_locked()
    # Unknown platform → fail-safe.
    _log.info("unknown platform %r — treating as locked (no capture)", sys.platform)
    return True


def _macos_locked() -> bool:
    """macOS: CGSessionCopyCurrentDictionary → CGSSessionScreenIsLocked."""
    try:
        from Quartz import CGSessionCopyCurrentDictionary  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — Quartz missing or import error
        _log.info("Quartz unavailable — treating as locked (fail-safe)")
        return True
    try:
        d = CGSessionCopyCurrentDictionary()
        if d is None:
            return True
        return bool(d.get("CGSSessionScreenIsLocked", 0))
    except Exception:  # noqa: BLE001
        return True


def _linux_locked() -> bool:
    """Linux: probe via xdg-screensaver status with exact-match.

    NB: xdg-screensaver tells us if the screensaver is enabled, not if
    the screen is currently locked. We treat the literal word ``active``
    (whitespace-stripped, lowercased, exact) as locked. ``inactive`` and
    everything else is unlocked.

    A more accurate Linux lock-detect would query ``loginctl show-session``
    for ``LockedHint`` — deferred to a follow-up since it requires
    parsing systemd output.
    """
    try:
        result = subprocess.run(
            ["xdg-screensaver", "status"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip().lower() == "active"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _log.info("xdg-screensaver unavailable — treating as locked (fail-safe)")
        return True


def _windows_locked() -> bool:
    """Windows: check user32 OpenInputDesktop. If we can't open the input
    desktop, the workstation is likely locked.
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        h = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if h == 0:
            return True
        user32.CloseDesktop(h)
        return False
    except Exception:  # noqa: BLE001
        return True


__all__ = ["is_screen_locked"]
