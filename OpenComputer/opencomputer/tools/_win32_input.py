"""Win32 ``SendInput`` shim for stock-Windows mouse + keyboard injection.

Why this exists: ``opencomputer[gui]`` brings ``pyautogui`` which works
everywhere but is a 50+ MB dep with PIL/Pillow. For Windows-only stock
installs we want a zero-dep fallback. ``ctypes`` + ``user32.dll`` is in
the stdlib on Windows.

All public functions return ``False`` on non-Windows so callers can chain
``win32_click_at(...) or pyautogui_click(...)`` without explicit
``sys.platform`` checks at every site.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

# Constants from WinUser.h
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010

_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_KEYUP = 0x0002


def _load_user32() -> Any:
    """Return ``ctypes.WinDLL('user32')``. Pulled out for testability."""
    if sys.platform != "win32":
        return None
    return ctypes.WinDLL("user32", use_last_error=True)


# Module-level structure definitions — defined inside `if sys.platform`
# guard so non-Windows imports don't try to resolve `wintypes` types
# that wintypes still provides cross-platform anyway. Keeping at module
# level avoids re-creating the structs on every call (R6).

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _InputUnion(ctypes.Union):  # noqa: N801 — ctypes pattern; suffix "Union" is intentional
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):  # noqa: N801 — mirrors WinUser.h INPUT struct name
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


def click_at(x: int, y: int, *, button: str, double: bool) -> bool:
    """Move the cursor to (x, y) and inject a click. Returns False on non-Windows."""
    if sys.platform != "win32":
        return False
    user32 = _load_user32()
    if user32 is None:
        return False

    if not user32.SetCursorPos(x, y):
        return False

    down = _MOUSEEVENTF_RIGHTDOWN if button == "right" else _MOUSEEVENTF_LEFTDOWN
    up = _MOUSEEVENTF_RIGHTUP if button == "right" else _MOUSEEVENTF_LEFTUP

    clicks = 2 if double else 1
    events = []
    for _ in range(clicks):
        for flag in (down, up):
            inp = _INPUT()
            inp.type = _INPUT_MOUSE
            inp.mi.dx = 0
            inp.mi.dy = 0
            inp.mi.mouseData = 0
            inp.mi.dwFlags = flag
            inp.mi.time = 0
            inp.mi.dwExtraInfo = None
            events.append(inp)

    n = len(events)
    arr = (_INPUT * n)(*events)
    sent = user32.SendInput(n, arr, ctypes.sizeof(_INPUT))
    return sent == n


def type_text(text: str) -> bool:
    """Inject Unicode text via repeated KEYEVENTF_UNICODE SendInput. False on non-Windows."""
    if sys.platform != "win32":
        return False
    user32 = _load_user32()
    if user32 is None:
        return False

    events = []
    for ch in text:
        for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            inp = _INPUT()
            inp.type = _INPUT_KEYBOARD
            inp.ki.wVk = 0
            inp.ki.wScan = ord(ch)
            inp.ki.dwFlags = flags
            inp.ki.time = 0
            inp.ki.dwExtraInfo = None
            events.append(inp)

    n = len(events)
    arr = (_INPUT * n)(*events)
    sent = user32.SendInput(n, arr, ctypes.sizeof(_INPUT))
    return sent == n


def send_keys(keys: list[str]) -> bool:
    """Inject a hotkey combination (e.g. ``["ctrl", "c"]``). Stub for follow-up.

    The mapping from string names → VK codes is non-trivial (see
    WinUser.h). For this milestone we ship ``type_text`` (most common
    case) and leave hotkey-by-name as a follow-up. Returns False to
    signal "not implemented" so callers fall through to pyautogui.
    """
    return False


__all__ = ["click_at", "type_text", "send_keys"]
