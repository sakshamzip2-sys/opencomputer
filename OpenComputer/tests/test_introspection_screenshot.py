"""tests/test_introspection_screenshot.py

ScreenshotTool: capture → vision-model analysis → JSON tool result.
The tool captures a screenshot, calls a vision model internally, and
returns JSON with ``{success, analysis, screenshot_path, ...}``. The
agent that called the tool sees TEXT in conversation history — never
the raw image bytes — keeping per-turn token cost flat.
"""
from __future__ import annotations

import json
import time
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


async def _fake_vision_success(*, image_b64, mime, prompt, api_key, model, max_tokens=1024):
    """Stand-in for analyze_image_bytes — happy path returns text."""
    return "A clean desktop with one terminal window open."


async def _fake_vision_failure(*, image_b64, mime, prompt, api_key, model, max_tokens=1024):
    """Stand-in for analyze_image_bytes — error path returns (msg, True)."""
    return ("vision API call failed: ConnectError: refused", True)


@pytest.mark.asyncio
async def test_full_screen_captures_and_runs_vision(tmp_path, monkeypatch):
    """Happy path: capture + vision call → JSON tool result.

    Tool result content is JSON: ``{success: true, analysis: <text>,
    screenshot_path: <path>, dimensions, size_kb}``. NO base64 in content.
    The PNG is on disk at the surfaced path.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    sct, _ = _fake_mss_context(size=(1920, 1080))

    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"PNGBYTES"), \
         patch("opencomputer.tools.vision_analyze.analyze_image_bytes", _fake_vision_success):
        tool = ScreenshotTool()
        result = await tool.execute(ToolCall(id="t1", name="screenshot", arguments={}))

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["success"] is True
    assert payload["analysis"] == "A clean desktop with one terminal window open."
    assert payload["dimensions"] == [1920, 1080]

    # Path is real, file exists, contents are the captured bytes — NOT base64.
    from pathlib import Path
    path = Path(payload["screenshot_path"])
    assert path.is_absolute() and path.exists()
    assert path.read_bytes() == b"PNGBYTES"
    assert path.is_relative_to(tmp_path / "tool_result_storage" / "screenshots")
    # Sanity: tool result content does NOT contain base64 of the PNG.
    import base64
    assert base64.b64encode(b"PNGBYTES").decode("ascii") not in result.content


@pytest.mark.asyncio
async def test_custom_prompt_steers_vision_analysis(tmp_path, monkeypatch):
    """The `prompt` parameter is forwarded to the vision call so the agent
    can ask focused questions ('what menu is open?', 'is this a captcha?')."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    sct, _ = _fake_mss_context()

    captured = {}
    async def _capture_prompt(*, image_b64, mime, prompt, api_key, model, max_tokens=1024):
        captured["prompt"] = prompt
        captured["mime"] = mime
        return "answer: it's a calculator"

    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"PNG"), \
         patch("opencomputer.tools.vision_analyze.analyze_image_bytes", _capture_prompt):
        tool = ScreenshotTool()
        await tool.execute(ToolCall(
            id="t1", name="screenshot",
            arguments={"prompt": "what app is in focus?"},
        ))

    assert captured["prompt"] == "what app is in focus?"
    assert captured["mime"] == "image/png"


@pytest.mark.asyncio
async def test_vision_failure_returns_path_for_user_share(tmp_path, monkeypatch):
    """Vision graceful-degradation.

    If the vision API fails but the capture succeeded, the result MUST
    include the ``screenshot_path`` so the agent can still share the
    file with the user via MEDIA:<path>. The tool result also includes
    a guidance ``note`` so the agent doesn't drop it on the floor.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    sct, _ = _fake_mss_context()

    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"PNG"), \
         patch("opencomputer.tools.vision_analyze.analyze_image_bytes", _fake_vision_failure):
        tool = ScreenshotTool()
        result = await tool.execute(ToolCall(id="t1", name="screenshot", arguments={}))

    payload = json.loads(result.content)
    assert payload["success"] is False
    assert "vision API call failed" in payload["error"]
    assert "screenshot_path" in payload
    assert "MEDIA:" in payload["note"]
    # File still exists on disk
    from pathlib import Path
    assert Path(payload["screenshot_path"]).exists()


@pytest.mark.asyncio
async def test_no_api_key_skips_vision_keeps_path(tmp_path, monkeypatch):
    """If ANTHROPIC_API_KEY isn't set, the tool still captures + returns
    the path. The analysis field carries a clear "skipped" message so
    the agent doesn't think vision succeeded."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sct, _ = _fake_mss_context()

    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"PNG"):
        tool = ScreenshotTool()
        result = await tool.execute(ToolCall(id="t1", name="screenshot", arguments={}))

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["success"] is True
    assert "skipped" in payload["analysis"].lower()
    from pathlib import Path
    assert Path(payload["screenshot_path"]).exists()


@pytest.mark.asyncio
async def test_capture_failure_returns_error_no_path(tmp_path, monkeypatch):
    """If the screenshot capture itself fails (no display, no permissions),
    the tool returns is_error=True with no screenshot_path — there's
    nothing to share."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    sct = MagicMock()
    sct.__enter__.side_effect = RuntimeError("no display")

    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct):
        tool = ScreenshotTool()
        result = await tool.execute(ToolCall(id="t1", name="screenshot", arguments={}))

    assert result.is_error
    payload = json.loads(result.content)
    assert payload["success"] is False
    assert "no display" in payload["error"]
    assert "screenshot_path" not in payload


@pytest.mark.asyncio
async def test_full_screen_grabs_primary_monitor(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sct, _ = _fake_mss_context()
    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"P"):
        tool = ScreenshotTool()
        await tool.execute(ToolCall(id="t1", name="screenshot", arguments={}))

    args = sct.grab.call_args[0][0]
    assert args == {"left": 0, "top": 0, "width": 100, "height": 100}


@pytest.mark.asyncio
@pytest.mark.parametrize("quadrant,expected", [
    ("top-left", {"left": 0, "top": 0, "width": 50, "height": 50}),
    ("top-right", {"left": 50, "top": 0, "width": 50, "height": 50}),
    ("bottom-left", {"left": 0, "top": 50, "width": 50, "height": 50}),
    ("bottom-right", {"left": 50, "top": 50, "width": 50, "height": 50}),
])
async def test_quadrant_uses_partial_bounds(tmp_path, monkeypatch, quadrant, expected):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sct, _ = _fake_mss_context()
    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"P"):
        tool = ScreenshotTool()
        await tool.execute(ToolCall(
            id="t1", name="screenshot", arguments={"quadrant": quadrant},
        ))

    assert sct.grab.call_args[0][0] == expected


@pytest.mark.asyncio
async def test_quadrant_with_offset_monitor_preserves_origin(tmp_path, monkeypatch):
    """Multi-monitor: primary may have non-zero left/top; quadrants must respect."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sct, _ = _fake_mss_context(monitors=[
        None, {"left": 1920, "top": 100, "width": 100, "height": 100},
    ])
    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"P"):
        tool = ScreenshotTool()
        await tool.execute(ToolCall(
            id="t1", name="screenshot",
            arguments={"quadrant": "bottom-right"},
        ))

    assert sct.grab.call_args[0][0] == {
        "left": 1970, "top": 150, "width": 50, "height": 50,
    }


def test_capability_claim_namespace():
    claims = ScreenshotTool.capability_claims
    assert claims[0].capability_id == "introspection.screenshot"


@pytest.mark.asyncio
async def test_prune_removes_old_screenshots(tmp_path, monkeypatch):
    """24h cleanup deletes stale screenshots on each capture."""
    import os as _os
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    screenshots_dir = tmp_path / "tool_result_storage" / "screenshots"
    screenshots_dir.mkdir(parents=True)
    stale = screenshots_dir / "oc-screen-stale.png"
    stale.write_bytes(b"old")
    old_mtime = time.time() - (25 * 60 * 60)
    _os.utime(stale, (old_mtime, old_mtime))
    fresh = screenshots_dir / "oc-screen-fresh.png"
    fresh.write_bytes(b"new")

    sct, _ = _fake_mss_context()
    with patch("extensions.coding_harness.introspection.tools.mss.mss", return_value=sct), \
         patch("extensions.coding_harness.introspection.tools.mss.tools.to_png", return_value=b"PNG"):
        tool = ScreenshotTool()
        await tool.execute(ToolCall(id="t1", name="screenshot", arguments={}))

    assert not stale.exists()
    assert fresh.exists()
