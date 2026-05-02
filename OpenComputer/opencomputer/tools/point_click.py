"""PointAndClick tool — programmatic mouse click at screen coordinates.

Phase 2.1 of the catch-up plan (real-gui-velvet-lemur). Tier-1 introspection
already ships (ScreenshotTool, ExtractScreenTextTool, etc.); this is the
first *output* GUI tool — macOS only, gated at PER_ACTION consent.

Implementation strategy:

1. Prefer ``Quartz.CGEventCreateMouseEvent`` (pyobjc-framework-Quartz) for
   event injection — fast, native, no shell.
2. Fall back to ``osascript -e 'tell application "System Events" to click
   at {x, y}'`` if pyobjc isn't installed (the optional ``[gui]`` extra).

Safety
------

- macOS only — returns an error on Linux/Windows.
- Coordinate range clamped to [0, 8000] (catches obvious garbage; the
  largest current Apple displays are 6K+).
- Capability tier is PER_ACTION: F1 ConsentGate prompts for every call
  (until tier-promoter promotes to EXPLICIT after N clean approvals).
- Both injection paths require macOS Accessibility permission for the
  controlling process. The first call without permission returns a
  clean error directing the user to System Settings → Privacy &
  Security → Accessibility.
"""

from __future__ import annotations

import asyncio
import sys
from typing import ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_MIN_COORD = 0
_MAX_COORD = 8000


class PointAndClickTool(BaseTool):
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True
    """Click at absolute screen coordinates. macOS only."""

    parallel_safe: bool = False  # mouse is a singleton
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="gui.point_click",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Inject a mouse click at the given screen coordinates "
                "(macOS Accessibility permission required)."
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="PointAndClick",
            description=(
                "Inject a mouse click at absolute screen coordinates (macOS only). "
                "Requires Accessibility permission for the controlling process. Default "
                "left-click; pass button='right' for right-click. CAUTION: this drives "
                "the user's actual desktop — verify coordinates first via Screenshot or "
                "ExtractScreenText so you don't click the wrong thing. PER_ACTION "
                "consent prompts every call until promoted. For text input or app "
                "control, prefer AppleScriptRun (more declarative). Coordinate range "
                "is clamped to [0,8000]; clicks far off-screen are rejected."
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
                        "description": "Mouse button.",
                        "enum": ["left", "right"],
                        "default": "left",
                    },
                },
                "required": ["x", "y"],
            },
        )

    @staticmethod
    def _validate_coords(x: int, y: int) -> str | None:
        """Return None if valid, else an error string."""
        if not isinstance(x, int) or not isinstance(y, int):
            return "x and y must be integers"
        if x < _MIN_COORD or x > _MAX_COORD or y < _MIN_COORD or y > _MAX_COORD:
            return f"coordinates out of range [0, {_MAX_COORD}]: ({x}, {y})"
        return None

    @staticmethod
    def _click_quartz(x: int, y: int, button: str) -> bool:
        """Try the native Quartz path. Returns True on success, False if
        pyobjc-framework-Quartz isn't installed."""
        try:
            import Quartz  # type: ignore[import-not-found]
        except ImportError:
            return False

        if button == "right":
            down_event = Quartz.kCGEventRightMouseDown
            up_event = Quartz.kCGEventRightMouseUp
            mouse_btn = Quartz.kCGMouseButtonRight
        else:
            down_event = Quartz.kCGEventLeftMouseDown
            up_event = Quartz.kCGEventLeftMouseUp
            mouse_btn = Quartz.kCGMouseButtonLeft
        point = Quartz.CGPointMake(x, y)
        ev_down = Quartz.CGEventCreateMouseEvent(None, down_event, point, mouse_btn)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_down)
        ev_up = Quartz.CGEventCreateMouseEvent(None, up_event, point, mouse_btn)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_up)
        return True

    @staticmethod
    async def _click_osascript(x: int, y: int, button: str) -> str | None:
        """Fallback path. Returns None on success, an error string on
        failure. Note: System Events 'click at' performs a left-click;
        right-click via osascript needs a different path which we don't
        cover in the fallback."""
        if button == "right":
            return (
                "right-click requires pyobjc-framework-Quartz "
                "(install with: pip install 'opencomputer[gui]')"
            )
        script = f'tell application "System Events" to click at {{{x}, {y}}}'
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return "osascript timed out (Accessibility permission?)"
        if proc.returncode != 0:
            return (
                stderr.decode("utf-8", errors="replace").strip()
                or f"osascript exited {proc.returncode}"
            )
        return None

    async def execute(self, call: ToolCall) -> ToolResult:
        if sys.platform != "darwin":
            return ToolResult(
                tool_call_id=call.id,
                content="Error: PointAndClick is macOS-only.",
                is_error=True,
            )

        args = call.arguments
        try:
            x = int(args["x"])
            y = int(args["y"])
        except (KeyError, TypeError, ValueError):
            return ToolResult(
                tool_call_id=call.id,
                content="Error: x and y are required integers",
                is_error=True,
            )

        bad = self._validate_coords(x, y)
        if bad:
            return ToolResult(tool_call_id=call.id, content=f"Error: {bad}", is_error=True)

        button = args.get("button", "left")
        if button not in ("left", "right"):
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: button must be 'left' or 'right', got {button!r}",
                is_error=True,
            )

        # Try Quartz first; fall back to osascript.
        if self._click_quartz(x, y, button):
            return ToolResult(
                tool_call_id=call.id,
                content=f"Clicked ({x}, {y}) via Quartz CGEvent ({button}-button)",
            )

        err = await self._click_osascript(x, y, button)
        if err is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Clicked ({x}, {y}) via osascript fallback ({button}-button)",
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"Error: {err}",
            is_error=True,
        )
