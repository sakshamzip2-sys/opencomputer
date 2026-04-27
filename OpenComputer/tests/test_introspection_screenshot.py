"""tests/test_introspection_screenshot.py"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest
from extensions.coding_harness.introspection.tools import ScreenshotTool

from plugin_sdk.core import ToolCall


def _fake_mss_context(rgb=b"\x00" * 12, size=(2, 2), monitors=None):
    """Return a MagicMock that behaves like an mss.mss() context manager."""
    monitors = monitors or [None, {"left": 0, "top": 0, "width": 100, "height": 100}]
    fake_grab = MagicMock(rgb=rgb, size=size)
    sct = MagicMock()
    sct.__enter__.return_value = sct
    sct.__exit__.return_value = False
    sct.monitors = monitors
    sct.grab.return_value = fake_grab
    return sct, fake_grab


@pytest.mark.asyncio
async def test_full_screen_returns_base64_png():
    sct, _ = _fake_mss_context()
    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"PNGDATA"):
        tool = ScreenshotTool()
        result = await tool.execute(ToolCall(id="t1", name="screenshot", arguments={}))

    assert not result.is_error
    assert base64.b64decode(result.content) == b"PNGDATA"


@pytest.mark.asyncio
async def test_full_screen_grabs_primary_monitor():
    sct, _ = _fake_mss_context()
    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"P"):
        tool = ScreenshotTool()
        await tool.execute(ToolCall(id="t1", name="screenshot", arguments={}))

    # No quadrant -> grabs sct.monitors[1] (primary) verbatim
    args = sct.grab.call_args[0][0]
    assert args == {"left": 0, "top": 0, "width": 100, "height": 100}


@pytest.mark.asyncio
@pytest.mark.parametrize("quadrant,expected", [
    ("top-left", {"left": 0, "top": 0, "width": 50, "height": 50}),
    ("top-right", {"left": 50, "top": 0, "width": 50, "height": 50}),
    ("bottom-left", {"left": 0, "top": 50, "width": 50, "height": 50}),
    ("bottom-right", {"left": 50, "top": 50, "width": 50, "height": 50}),
])
async def test_quadrant_uses_partial_bounds(quadrant, expected):
    sct, _ = _fake_mss_context()
    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"P"):
        tool = ScreenshotTool()
        await tool.execute(ToolCall(id="t1", name="screenshot", arguments={"quadrant": quadrant}))

    assert sct.grab.call_args[0][0] == expected


@pytest.mark.asyncio
async def test_quadrant_with_offset_monitor_preserves_origin():
    """Multi-monitor setups: primary monitor may have non-zero left/top.
    Quadrant computation must respect that origin."""
    sct, _ = _fake_mss_context(monitors=[None, {"left": 1920, "top": 100, "width": 100, "height": 100}])
    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"P"):
        tool = ScreenshotTool()
        await tool.execute(ToolCall(id="t1", name="screenshot", arguments={"quadrant": "bottom-right"}))

    assert sct.grab.call_args[0][0] == {"left": 1970, "top": 150, "width": 50, "height": 50}


@pytest.mark.asyncio
async def test_capability_claim_namespace():
    claims = ScreenshotTool.capability_claims
    assert claims[0].capability_id == "introspection.screenshot"


@pytest.mark.asyncio
async def test_handles_mss_exception():
    sct = MagicMock()
    sct.__enter__.side_effect = RuntimeError("no display")
    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct):
        tool = ScreenshotTool()
        result = await tool.execute(ToolCall(id="t1", name="screenshot", arguments={}))

    assert result.is_error
    assert "no display" in result.content
