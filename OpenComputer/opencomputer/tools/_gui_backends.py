"""Shared backend dispatch helpers for cross-platform GUI tools.

Each ``SystemX`` tool calls these to (a) figure out which backend to use,
(b) detect whether a particular CLI / library is available. Pure helpers
— no side effects, no I/O at import time. The actual GUI ops live in
the tool modules that call into here.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Literal

Backend = Literal[
    "pyautogui", "xdotool", "ydotool", "quartz", "powershell", "osascript", "notify_send"
]
Platform = Literal["macos", "linux", "windows", "unknown"]
DisplayServer = Literal["x11", "wayland"]


def detect_platform() -> Platform:
    """Return the running platform classification."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    return "unknown"


def detect_linux_display_server() -> DisplayServer:
    """Return ``x11`` or ``wayland`` based on ``$XDG_SESSION_TYPE``.

    Defaults to ``x11`` when unset (the most common case on legacy Linux
    desktops). Caller is responsible for further detecting whether
    ``xdotool`` / ``ydotool`` are actually installed — this function only
    classifies the *display server*, not backend availability.
    """
    val = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if val == "wayland":
        return "wayland"
    return "x11"


def has_command(name: str) -> bool:
    """``shutil.which`` wrapper, for testability."""
    return shutil.which(name) is not None


def has_pyautogui() -> bool:
    """Whether ``pyautogui`` is importable.

    **Don't** call ``import pyautogui`` at module load time — pyautogui
    opens an X11 display on import on Linux, which is a side-effect we
    can't afford during plain ``from opencomputer.tools.system_click
    import SystemClickTool``. This helper uses ``importlib.util.find_spec``
    which is import-side-effect-free.
    """
    import importlib.util

    return importlib.util.find_spec("pyautogui") is not None


__all__ = [
    "Backend",
    "DisplayServer",
    "Platform",
    "detect_linux_display_server",
    "detect_platform",
    "has_command",
    "has_pyautogui",
]
