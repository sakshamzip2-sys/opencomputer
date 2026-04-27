"""Cross-platform foreground-app detection.

Each platform path returns ``None`` on failure rather than raising — the
caller (sensor daemon) treats that as "skip this tick" and tries again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ForegroundSnapshot:
    app_name: str
    window_title: str
    bundle_id: str
    platform: str


_OSASCRIPT = """
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    set appName to name of frontApp
    set bundleID to bundle identifier of frontApp
    try
        set winTitle to name of front window of frontApp
    on error
        set winTitle to ""
    end try
end tell
return appName & "\\n" & winTitle & "\\n" & bundleID
"""


def _detect_macos() -> ForegroundSnapshot | None:
    try:
        result = subprocess.run(
            ["osascript", "-e", _OSASCRIPT],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    parts = (result.stdout or "").rstrip("\n").split("\n", 2)
    while len(parts) < 3:
        parts.append("")
    return ForegroundSnapshot(
        app_name=parts[0].strip(),
        window_title=parts[1].strip(),
        bundle_id=parts[2].strip(),
        platform="darwin",
    )


def _detect_linux() -> ForegroundSnapshot | None:
    # Wayland-only sessions: WAYLAND_DISPLAY set + DISPLAY empty/unset.
    # We do not support Wayland in v1; the daemon will get None and skip.
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        return None

    if shutil.which("xdotool"):
        try:
            wid = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if wid.returncode != 0:
                return None
            wid_str = wid.stdout.strip()
            if not wid_str:
                return None
            title = subprocess.run(
                ["xdotool", "getwindowname", wid_str],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            klass = subprocess.run(
                ["xdotool", "getwindowclassname", wid_str],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            return ForegroundSnapshot(
                app_name=(klass.stdout or "").strip(),
                window_title=(title.stdout or "").strip(),
                bundle_id="",
                platform="linux",
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    if shutil.which("wmctrl"):
        try:
            result = subprocess.run(
                ["wmctrl", "-l"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            for line in (result.stdout or "").splitlines():
                # wmctrl format: <id> <desktop> <hostname> <title>
                parts = line.split(None, 3)
                if len(parts) == 4:
                    return ForegroundSnapshot(
                        app_name="",
                        window_title=parts[3],
                        bundle_id="",
                        platform="linux",
                    )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    return None


def _detect_windows() -> ForegroundSnapshot | None:
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        return None

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        title = win32gui.GetWindowText(hwnd) or ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            app = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            app = ""
        return ForegroundSnapshot(
            app_name=app,
            window_title=title,
            bundle_id="",
            platform="win32",
        )
    except Exception:  # noqa: BLE001 — Windows API quirks
        return None


def detect_foreground() -> ForegroundSnapshot | None:
    """Return a snapshot of the foreground app, or None if unavailable."""
    if sys.platform == "darwin":
        return _detect_macos()
    if sys.platform.startswith("linux"):
        return _detect_linux()
    if sys.platform == "win32":
        return _detect_windows()
    return None
