"""Best-effort macOS frontmost app detection. Returns "" on non-macOS or failure."""
from __future__ import annotations

import shutil
import subprocess


def detect_frontmost_app() -> str:
    """Use osascript to query the System Events frontmost app. Empty string on failure."""
    if shutil.which("osascript") is None:
        return ""
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first application process whose frontmost is true',
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=2.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def detect_window_title() -> str:
    """Return the front window's title via osascript.

    v2 addition (2026-05-01) — catches sub-app context that
    ``detect_frontmost_app`` misses (e.g. Chrome's app name is
    "Google Chrome" regardless of which site is open, but the window
    title contains the page title which is the actual signal).

    Returns "" on non-macOS, no front window, or any failure.
    """
    if shutil.which("osascript") is None:
        return ""
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                "tell application \"System Events\" to "
                "set frontApp to name of first application process "
                "whose frontmost is true",
                "-e",
                "tell application frontApp to "
                "if (count of windows) > 0 then "
                "return name of front window",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=2.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
