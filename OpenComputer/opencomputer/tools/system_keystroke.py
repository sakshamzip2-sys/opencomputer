"""SystemKeystroke — cross-platform type-text or hotkey tool.

Backend dispatch:
- pyautogui (cross-platform, if [gui] extra installed) for both text + hotkey
- macOS:  osascript fallback for text
- Linux:  xdotool / ydotool fallback for both
- Windows: pyautogui only (no shell fallback)

API: pass EITHER ``text`` OR ``hotkey``, not both. Hotkey format mirrors
pyautogui's ``hotkey(*keys)`` — comma-separated key names like
``"ctrl,c"``, ``"cmd,space"``.

Safety: PER_ACTION consent. Text capped at 4000 chars. parallel_safe=False.
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

_MAX_TEXT_LEN = 4000


class SystemKeystrokeTool(BaseTool):
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True
    """Type text or send a hotkey combination. Cross-platform."""

    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="gui.system_keystroke",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Inject keyboard events: type text or press a hotkey "
                "combination. Cross-platform."
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SystemKeystroke",
            description=(
                "Type text or press a hotkey combination on the active window. "
                "Cross-platform (macOS / Linux / Windows). Pass `text` to type a "
                "string (≤4000 chars). Pass `hotkey` (comma-separated key names) to "
                "press a combination, e.g. 'ctrl,c' or 'cmd,space'. Exactly one of "
                "the two must be set. PER_ACTION consent."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {
                        "type": "string",
                        "description": f"Text to type (≤{_MAX_TEXT_LEN} chars).",
                        "maxLength": _MAX_TEXT_LEN,
                    },
                    "hotkey": {
                        "type": "string",
                        "description": "Comma-separated key names, e.g. 'ctrl,c'.",
                    },
                },
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        text = args.get("text")
        hotkey = args.get("hotkey")

        if not text and not hotkey:
            return ToolResult(tool_call_id=call.id, content="must pass either text or hotkey", is_error=True)
        if text and hotkey:
            return ToolResult(tool_call_id=call.id, content="text and hotkey are mutually exclusive", is_error=True)
        if text and len(text) > _MAX_TEXT_LEN:
            return ToolResult(tool_call_id=call.id, content=f"text exceeds {_MAX_TEXT_LEN}-char cap", is_error=True)

        platform = detect_platform()
        try:
            if text:
                ok = await asyncio.to_thread(_type_dispatch, platform, text)
                desc = f"typed {len(text)} chars"
            else:
                keys = [k.strip() for k in hotkey.split(",") if k.strip()]
                if not keys:
                    return ToolResult(tool_call_id=call.id, content="hotkey is empty after parsing", is_error=True)
                ok = await asyncio.to_thread(_hotkey_dispatch, platform, keys)
                desc = f"hotkey {'+'.join(keys)}"
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"keystroke failed: {type(exc).__name__}: {exc}", is_error=True)

        if not ok:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "no backend available. Install with `pip install "
                    "opencomputer[gui]`, or install xdotool (Linux X11) / "
                    "ydotool (Linux Wayland)."
                ),
                is_error=True,
            )
        return ToolResult(tool_call_id=call.id, content=desc)


def _type_dispatch(platform: str, text: str) -> bool:
    # Windows-first: try the zero-dep ctypes shim before pyautogui so
    # stock-Windows installs (no `[gui]` extra) still work. macos/linux
    # branches unchanged.
    if platform == "windows" and _type_win32_sendinput(text):
        return True
    if platform in ("macos", "linux", "windows") and _type_pyautogui(text):
        return True
    if platform == "linux":
        return _type_xdotool(text)
    if platform == "macos":
        return _type_osascript(text)
    return False


def _type_win32_sendinput(text: str) -> bool:
    """Stock-Windows ctypes SendInput. Returns False on non-Windows."""
    from opencomputer.tools._win32_input import type_text
    return type_text(text)


def _hotkey_dispatch(platform: str, keys: list[str]) -> bool:
    if platform in ("macos", "linux", "windows") and _hotkey_pyautogui(keys):
        return True
    if platform == "linux":
        return _hotkey_xdotool(keys)
    return False


def _type_pyautogui(text: str) -> bool:
    if not has_pyautogui():
        return False
    try:
        import pyautogui  # type: ignore[import-not-found]

        pyautogui.write(text)
        return True
    except Exception:  # noqa: BLE001
        return False


def _hotkey_pyautogui(keys: list[str]) -> bool:
    if not has_pyautogui():
        return False
    try:
        import pyautogui  # type: ignore[import-not-found]

        pyautogui.hotkey(*keys)
        return True
    except Exception:  # noqa: BLE001
        return False


def _type_xdotool(text: str) -> bool:
    import subprocess

    server = detect_linux_display_server()
    if server == "wayland" and has_command("ydotool"):
        cmd = ["ydotool", "type", text]
    elif has_command("xdotool"):
        cmd = ["xdotool", "type", "--", text]
    else:
        return False
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return out.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _hotkey_xdotool(keys: list[str]) -> bool:
    import subprocess

    server = detect_linux_display_server()
    combo = "+".join(keys)
    if server == "wayland" and has_command("ydotool"):
        cmd = ["ydotool", "key", combo]
    elif has_command("xdotool"):
        cmd = ["xdotool", "key", combo]
    else:
        return False
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _type_osascript(text: str) -> bool:
    """macOS AppleScript text injection — `keystroke` of the active app."""
    import subprocess

    if not has_command("osascript"):
        return False
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "System Events" to keystroke "{escaped}"'
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


__all__ = ["SystemKeystrokeTool"]
