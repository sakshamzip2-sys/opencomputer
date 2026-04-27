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
