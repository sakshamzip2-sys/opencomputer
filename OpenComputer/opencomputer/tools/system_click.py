"""SystemClick — cross-platform mouse-click tool.

Backend dispatch (in order of preference):

- macOS:   Quartz (pyobjc) → pyautogui → osascript
- Linux:   pyautogui → xdotool (X11) / ydotool (Wayland)
- Windows: pyautogui

Without ``pip install opencomputer[gui]``, only the platform-native
shell-outs are available. Add the extra to get the single cross-platform
``pyautogui`` fallback that works the same everywhere.

Safety: PER_ACTION consent (F1 ConsentGate prompts every call). Coords
clamped to [0, 8000]. ``parallel_safe = False`` (mouse is a singleton).
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from opencomputer.tools._gui_backends import (
    detect_linux_display_server,
    detect_platform,
    has_command,
    has_pyautogui,
)
from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_MIN_COORD = 0
_MAX_COORD = 8000


class SystemClickTool(BaseTool):
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True
    """Inject a mouse click at absolute screen coordinates. Cross-platform."""

    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="gui.system_click",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Inject a mouse click at the given screen coordinates "
                "(cross-platform: macOS / Linux / Windows). Requires "
                "OS-level accessibility / input permission."
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SystemClick",
            description=(
                "Inject a mouse click at absolute screen coordinates. Works on "
                "macOS / Linux (X11 + Wayland) / Windows. Backend chain: native API "
                "→ pyautogui (if [gui] extra installed) → platform CLI fallback "
                "(xdotool / ydotool / osascript). PER_ACTION consent. Coordinate "
                "range [0, 8000]. Set button='right' for right-click; double=true "
                "for double-click. Requires OS accessibility / input permission "
                "(macOS: Privacy & Security → Accessibility; Linux: input group; "
                "Windows: usually allowed by default)."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "x": {
                        "type": "integer",
                        "description": f"Screen X (0..{_MAX_COORD}).",
                        "minimum": _MIN_COORD,
                        "maximum": _MAX_COORD,
                    },
                    "y": {
                        "type": "integer",
                        "description": f"Screen Y (0..{_MAX_COORD}).",
                        "minimum": _MIN_COORD,
                        "maximum": _MAX_COORD,
                    },
                    "button": {
                        "type": "string",
                        "enum": ["left", "right"],
                    },
                    "double": {"type": "boolean"},
                },
                "required": ["x", "y"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        x, y = args.get("x"), args.get("y")
        button = args.get("button", "left")
        double = bool(args.get("double", False))

        if not isinstance(x, int) or not isinstance(y, int):
            return ToolResult(tool_call_id=call.id, content="x and y must be integers", is_error=True)
        if x < _MIN_COORD or x > _MAX_COORD or y < _MIN_COORD or y > _MAX_COORD:
            return ToolResult(
                tool_call_id=call.id,
                content=f"coordinates out of range [{_MIN_COORD}, {_MAX_COORD}]: ({x}, {y})",
                is_error=True,
            )
        if button not in ("left", "right"):
            return ToolResult(tool_call_id=call.id, content=f"invalid button {button!r}", is_error=True)

        platform = detect_platform()
        try:
            ok = await asyncio.to_thread(_click_dispatch, platform, x, y, button, double)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"click failed: {type(exc).__name__}: {exc}", is_error=True)

        if not ok:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "no backend available. Install with `pip install "
                    "opencomputer[gui]` (cross-platform pyautogui), or install a "
                    "native fallback: xdotool (Linux X11) / ydotool (Linux "
                    "Wayland)."
                ),
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"clicked ({x}, {y}) button={button} double={double}",
        )


def _click_dispatch(platform: str, x: int, y: int, button: str, double: bool) -> bool:
    """Try each backend in preference order. First success wins."""
    if platform == "macos":
        return (
            _click_quartz(x, y, button, double)
            or _click_pyautogui(x, y, button, double)
            or _click_osascript(x, y, button, double)
        )
    if platform == "linux":
        return _click_pyautogui(x, y, button, double) or _click_xdotool(x, y, button, double)
    if platform == "windows":
        return (
            _click_win32_sendinput(x, y, button, double)
            or _click_pyautogui(x, y, button, double)
        )
    return False


def _click_win32_sendinput(x: int, y: int, button: str, double: bool) -> bool:
    """Stock-Windows ctypes SendInput. Zero-dep fallback before pyautogui."""
    from opencomputer.tools._win32_input import click_at
    return click_at(x, y, button=button, double=double)


def _click_pyautogui(x: int, y: int, button: str, double: bool) -> bool:
    if not has_pyautogui():
        return False
    try:
        import pyautogui  # type: ignore[import-not-found]

        if double:
            pyautogui.doubleClick(x, y, button=button)
        else:
            pyautogui.click(x, y, button=button)
        return True
    except Exception:  # noqa: BLE001
        return False


def _click_quartz(x: int, y: int, button: str, double: bool) -> bool:
    try:
        import Quartz  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        if button == "right":
            down = Quartz.kCGEventRightMouseDown
            up = Quartz.kCGEventRightMouseUp
            btn = Quartz.kCGMouseButtonRight
        else:
            down = Quartz.kCGEventLeftMouseDown
            up = Quartz.kCGEventLeftMouseUp
            btn = Quartz.kCGMouseButtonLeft
        clicks = 2 if double else 1
        for _ in range(clicks):
            ev_down = Quartz.CGEventCreateMouseEvent(None, down, (x, y), btn)
            ev_up = Quartz.CGEventCreateMouseEvent(None, up, (x, y), btn)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_up)
        return True
    except Exception:  # noqa: BLE001
        return False


def _click_xdotool(x: int, y: int, button: str, double: bool) -> bool:
    """Linux X11 (xdotool) / Wayland (ydotool) shell fallback."""
    import subprocess

    server = detect_linux_display_server()
    btn_num = "3" if button == "right" else "1"
    repeat = "2" if double else "1"

    if server == "wayland" and has_command("ydotool"):
        code = "0xC1" if button == "right" else "0xC0"
        cmd = ["ydotool", "click", code]
    elif has_command("xdotool"):
        cmd = ["xdotool", "mousemove", str(x), str(y), "click", "--repeat", repeat, btn_num]
    else:
        return False

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _click_osascript(x: int, y: int, button: str, double: bool) -> bool:
    """macOS AppleScript fallback. Left-click only."""
    import subprocess

    if not has_command("osascript"):
        return False
    if button != "left":
        return False
    script = f'tell application "System Events" to click at {{{x}, {y}}}'
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


__all__ = ["SystemClickTool"]
