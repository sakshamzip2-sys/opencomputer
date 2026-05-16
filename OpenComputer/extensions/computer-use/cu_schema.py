"""Schema for the generic ``computer_use`` tool.

Ported verbatim from hermes-agent ``tools/computer_use/schema.py``.

Model-agnostic. Any tool-calling model can drive this. Vision-capable models
should prefer ``capture(mode='som')`` then ``click(element=N)`` — much more
reliable than pixel coordinates. Pixel coordinates remain supported for
models that were trained on them (e.g. Claude's computer-use RL).
"""

from __future__ import annotations

from typing import Any

# One consolidated tool with an `action` discriminator. Keeps the schema
# compact and the per-turn token cost low.
COMPUTER_USE_SCHEMA: dict[str, Any] = {
    "name": "computer_use",
    "description": (
        "Drive the macOS desktop in the background — screenshots, mouse, "
        "keyboard, scroll, drag — without stealing the user's cursor, "
        "keyboard focus, or Space. Preferred workflow: call with "
        "action='capture' (mode='som' returns a screenshot plus an indexed "
        "list of every interactable element), then click by `element` index "
        "for reliability. Pixel coordinates are supported for models trained "
        "on them. Works on any window — hidden, minimized, on another Space, "
        "or behind another app. macOS only; requires cua-driver to be "
        "installed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "capture",
                    "click",
                    "double_click",
                    "right_click",
                    "middle_click",
                    "drag",
                    "scroll",
                    "type",
                    "key",
                    "set_value",
                    "wait",
                    "list_apps",
                    "focus_app",
                ],
                "description": (
                    "Which action to perform. `capture` is free (no side "
                    "effects). All other actions require approval unless "
                    "auto-approved. Use `set_value` for select/popup elements "
                    "and sliders — it selects the matching option directly "
                    "without opening the native menu (no focus steal)."
                ),
            },
            # ── capture ────────────────────────────────────────────
            "mode": {
                "type": "string",
                "enum": ["som", "vision", "ax"],
                "description": (
                    "Capture mode. `som` (default) returns a screenshot plus "
                    "an indexed list of every interactable element (role, "
                    "label, 1-based index) walked from the accessibility "
                    "tree — best for vision models, lets you click by "
                    "element index. The numbers are NOT drawn onto the "
                    "image; correlate by role/label from the element list. "
                    "`vision` is a plain screenshot, no element list. "
                    "`ax` is the element list only (no image; useful for "
                    "text-only models)."
                ),
            },
            "app": {
                "type": "string",
                "description": (
                    "Optional. Limit capture/action to a specific app "
                    "(by name, e.g. 'Safari', or bundle ID, "
                    "'com.apple.Safari'). If omitted, operates on the "
                    "frontmost app's window or the whole screen."
                ),
            },
            # ── click / drag / scroll targeting ────────────────────
            "element": {
                "type": "integer",
                "description": (
                    "The 1-based SOM index returned by the last "
                    "`capture(mode='som')` call. Strongly preferred over "
                    "raw coordinates."
                ),
            },
            "coordinate": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "Pixel coordinates [x, y] in WINDOW-LOCAL SCREENSHOT "
                    "pixels — the space of the PNG the last `capture` "
                    "returned, top-left origin, sized by that capture's "
                    "`width`/`height`. NOT logical screen points: on a "
                    "Retina display screenshot pixels are 2x the logical "
                    "points, so read the coordinate off the capture image, "
                    "not the physical screen. Only use this if no element "
                    "index is available."
                ),
            },
            "button": {
                "type": "string",
                "enum": ["left", "right", "middle"],
                "description": "Mouse button. Defaults to left.",
            },
            "modifiers": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["cmd", "shift", "option", "alt", "ctrl", "fn"],
                },
                "description": "Modifier keys held during the action.",
            },
            # ── drag ───────────────────────────────────────────────
            # NOTE: drag is pixel-only on the cua-driver backend (macOS AX
            # has no semantic drag action). These element fields are kept
            # for forward compatibility but are rejected at dispatch — pass
            # from_coordinate / to_coordinate instead.
            "from_element": {"type": "integer",
                              "description": "Source element index. UNSUPPORTED "
                              "for drag (pixel-only) — use from_coordinate."},
            "to_element": {"type": "integer",
                            "description": "Target element index. UNSUPPORTED "
                            "for drag (pixel-only) — use to_coordinate."},
            "from_coordinate": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2, "maxItems": 2,
                "description": (
                    "Drag-start [x,y] in window-local screenshot pixels "
                    "(same space as `coordinate`). drag is pixel-only — "
                    "element-indexed drag is not supported."
                ),
            },
            "to_coordinate": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2, "maxItems": 2,
                "description": (
                    "Drag-end [x,y] in window-local screenshot pixels "
                    "(same space as `coordinate`). drag is pixel-only — "
                    "element-indexed drag is not supported."
                ),
            },
            # ── scroll ─────────────────────────────────────────────
            "direction": {
                "type": "string",
                "enum": ["up", "down", "left", "right"],
                "description": "Scroll direction.",
            },
            "amount": {
                "type": "integer",
                "description": "Scroll wheel ticks. Default 3.",
            },
            # ── set_value ──────────────────────────────────────────
            "value": {
                "type": "string",
                "description": (
                    "For action='set_value': the value to set on the element. "
                    "For AXPopUpButton / select dropdowns, pass the option's "
                    "display label (e.g. 'Blue'). For sliders and other "
                    "AXValue-settable elements, pass the numeric or string value."
                ),
            },
            # ── type / key / wait ──────────────────────────────────
            "text": {
                "type": "string",
                "description": "Text to type (respects the current layout).",
            },
            "keys": {
                "type": "string",
                "description": (
                    "Key combo, e.g. 'cmd+s', 'ctrl+alt+t', 'return', "
                    "'escape', 'tab'. Use '+' to combine."
                ),
            },
            "seconds": {
                "type": "number",
                "description": "Seconds to wait. Max 30.",
            },
            # ── focus_app ──────────────────────────────────────────
            "raise_window": {
                "type": "boolean",
                "description": (
                    "Only for action='focus_app'. If true, brings the "
                    "window to front (DISRUPTS the user). Default false "
                    "— input is routed to the app without raising, "
                    "matching the background co-work model."
                ),
            },
            # ── return shape ───────────────────────────────────────
            "capture_after": {
                "type": "boolean",
                "description": (
                    "If true, take a follow-up capture after the action "
                    "and include it in the response. Saves a round-trip "
                    "when you need to verify an action's effect."
                ),
            },
        },
        "required": ["action"],
    },
}


def get_computer_use_schema() -> dict[str, Any]:
    """Return the generic OpenAI function-calling schema."""
    return COMPUTER_USE_SCHEMA


__all__ = ["COMPUTER_USE_SCHEMA", "get_computer_use_schema"]
