"""Tests for PointAndClickTool + AppleScriptRunTool (Phase 2.1 + 2.2).

These tests run on every platform — macOS-specific code paths are
mocked so CI (Linux runners) doesn't need pyobjc or osascript.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.tools.applescript_run import AppleScriptRunTool
from opencomputer.tools.point_click import PointAndClickTool
from plugin_sdk.consent import ConsentTier
from plugin_sdk.core import ToolCall

# =============================================================
# PointAndClickTool
# =============================================================


def test_point_click_capability_per_action():
    claims = PointAndClickTool.capability_claims
    assert len(claims) == 1
    assert claims[0].capability_id == "gui.point_click"
    assert claims[0].tier_required == ConsentTier.PER_ACTION


def test_point_click_schema_pascal_case():
    tool = PointAndClickTool()
    assert tool.schema.name == "PointAndClick"
    assert "x" in tool.schema.parameters["required"]
    assert "y" in tool.schema.parameters["required"]


def test_point_click_not_parallel_safe():
    assert PointAndClickTool.parallel_safe is False


def test_point_click_validate_coords_in_range():
    assert PointAndClickTool._validate_coords(0, 0) is None
    assert PointAndClickTool._validate_coords(8000, 8000) is None
    assert PointAndClickTool._validate_coords(500, 500) is None


def test_point_click_validate_coords_out_of_range():
    assert PointAndClickTool._validate_coords(-1, 0) is not None
    assert PointAndClickTool._validate_coords(8001, 0) is not None
    assert PointAndClickTool._validate_coords(0, -1) is not None
    assert PointAndClickTool._validate_coords(0, 8001) is not None


def test_point_click_rejects_non_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    tool = PointAndClickTool()
    call = ToolCall(id="t1", name="PointAndClick", arguments={"x": 100, "y": 200})
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "macOS-only" in result.content


def test_point_click_rejects_missing_coords(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    tool = PointAndClickTool()
    call = ToolCall(id="t2", name="PointAndClick", arguments={"x": 100})
    result = asyncio.run(tool.execute(call))
    assert result.is_error


def test_point_click_rejects_oob_coords(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    tool = PointAndClickTool()
    call = ToolCall(id="t3", name="PointAndClick", arguments={"x": 99999, "y": 0})
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "out of range" in result.content


def test_point_click_rejects_invalid_button(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    # bypass quartz so we don't accidentally click on the dev machine
    tool = PointAndClickTool()
    call = ToolCall(
        id="t4",
        name="PointAndClick",
        arguments={"x": 100, "y": 100, "button": "middle"},
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "left" in result.content or "right" in result.content


def test_point_click_uses_quartz_when_available(monkeypatch):
    """Happy path: pyobjc-framework-Quartz importable → native CGEvent path."""
    monkeypatch.setattr(sys, "platform", "darwin")

    # Build a fake Quartz module surface used by _click_quartz
    fake_quartz = MagicMock()
    fake_quartz.kCGEventLeftMouseDown = 1
    fake_quartz.kCGEventLeftMouseUp = 2
    fake_quartz.kCGMouseButtonLeft = 0
    fake_quartz.kCGHIDEventTap = 0
    fake_quartz.CGPointMake = lambda x, y: (x, y)
    fake_quartz.CGEventCreateMouseEvent = MagicMock(return_value="event")
    fake_quartz.CGEventPost = MagicMock()
    monkeypatch.setitem(sys.modules, "Quartz", fake_quartz)

    tool = PointAndClickTool()
    call = ToolCall(id="t5", name="PointAndClick", arguments={"x": 123, "y": 456})
    result = asyncio.run(tool.execute(call))
    assert not result.is_error
    assert "Quartz" in result.content
    assert fake_quartz.CGEventPost.call_count == 2  # down + up


def test_point_click_falls_back_to_osascript(monkeypatch):
    """If Quartz isn't installed, fallback to osascript."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "Quartz", None)  # ImportError on import

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.returncode = 0

    async def fake_create(*args, **kwargs):
        # First positional arg should be 'osascript'
        assert args[0] == "osascript"
        return fake_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
        tool = PointAndClickTool()
        call = ToolCall(id="t6", name="PointAndClick", arguments={"x": 10, "y": 20})
        result = asyncio.run(tool.execute(call))
    assert not result.is_error
    assert "osascript" in result.content


def test_point_click_right_button_via_quartz(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fake_quartz = MagicMock()
    fake_quartz.kCGEventRightMouseDown = 3
    fake_quartz.kCGEventRightMouseUp = 4
    fake_quartz.kCGMouseButtonRight = 1
    fake_quartz.kCGHIDEventTap = 0
    fake_quartz.CGPointMake = lambda x, y: (x, y)
    fake_quartz.CGEventCreateMouseEvent = MagicMock(return_value="event")
    fake_quartz.CGEventPost = MagicMock()
    monkeypatch.setitem(sys.modules, "Quartz", fake_quartz)

    tool = PointAndClickTool()
    call = ToolCall(
        id="t7", name="PointAndClick", arguments={"x": 1, "y": 2, "button": "right"}
    )
    result = asyncio.run(tool.execute(call))
    assert not result.is_error
    assert "right" in result.content


def test_point_click_right_button_osascript_unsupported(monkeypatch):
    """Fallback path doesn't support right-click — must report a clean error."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "Quartz", None)
    tool = PointAndClickTool()
    call = ToolCall(
        id="t8", name="PointAndClick", arguments={"x": 1, "y": 2, "button": "right"}
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "right-click requires" in result.content


# =============================================================
# AppleScriptRunTool
# =============================================================


def test_applescript_capability_per_action():
    claims = AppleScriptRunTool.capability_claims
    assert len(claims) == 1
    assert claims[0].capability_id == "gui.applescript_run"
    assert claims[0].tier_required == ConsentTier.PER_ACTION


def test_applescript_schema_pascal_case():
    tool = AppleScriptRunTool()
    assert tool.schema.name == "AppleScriptRun"
    assert "script" in tool.schema.parameters["required"]


@pytest.mark.parametrize("script,denied", [
    ('tell application "Finder" to empty trash', True),
    ('do shell script "shutdown -h now"', True),
    ('tell application "System Events" to restart', True),
    ('do shell script "rm -rf /tmp/foo"', True),
    # ``rm -r`` (recursive, even without force) is still destructive; the
    # denylist regex catches it. Test asserts the policy explicitly.
    ('do shell script "rm -r /tmp/foo"', True),
    ('display notification "hi"', False),
    ('tell application "Notes" to make new note', False),
    ('tell app "Music" to play', False),
    ('do shell script "ls /tmp"', False),  # non-destructive shell call OK
])
def test_applescript_denylist(script: str, denied: bool):
    bad = AppleScriptRunTool._denylist_check(script)
    if denied:
        assert bad is not None
    else:
        assert bad is None


def test_applescript_rejects_non_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    tool = AppleScriptRunTool()
    call = ToolCall(
        id="t9", name="AppleScriptRun", arguments={"script": 'display notification "hi"'}
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "macOS-only" in result.content


def test_applescript_rejects_empty_script(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    tool = AppleScriptRunTool()
    call = ToolCall(id="t10", name="AppleScriptRun", arguments={"script": "   "})
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "non-empty" in result.content


def test_applescript_dry_run_does_not_execute(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    called = []

    async def fake_create(*args, **kwargs):
        called.append(args)
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
        tool = AppleScriptRunTool()
        call = ToolCall(
            id="t11",
            name="AppleScriptRun",
            arguments={"script": 'display notification "hi"', "dry_run": True},
        )
        result = asyncio.run(tool.execute(call))
    assert not result.is_error
    assert "DRY RUN" in result.content
    assert called == []  # subprocess never invoked


def test_applescript_denylisted_returns_error(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    tool = AppleScriptRunTool()
    call = ToolCall(
        id="t12",
        name="AppleScriptRun",
        arguments={"script": 'tell application "Finder" to empty trash'},
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "denylisted" in result.content


def test_applescript_executes_safe_script(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    async def fake_create(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"hello\n", b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
        tool = AppleScriptRunTool()
        call = ToolCall(
            id="t13",
            name="AppleScriptRun",
            arguments={"script": 'return "hello"'},
        )
        result = asyncio.run(tool.execute(call))
    assert not result.is_error
    assert result.content == "hello"


def test_applescript_surfaces_nonzero_exit(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    async def fake_create(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b"syntax error"))
        proc.returncode = 1
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
        tool = AppleScriptRunTool()
        call = ToolCall(
            id="t14", name="AppleScriptRun", arguments={"script": "garbage"}
        )
        result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "syntax error" in result.content


def test_applescript_timeout_kills_process(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    async def slow_communicate(*_a, **_k):
        await asyncio.sleep(5)
        return b"", b""

    proc = MagicMock()
    proc.communicate = slow_communicate  # async function, awaited by tool
    proc.kill = MagicMock()

    async def fake_create(*args, **kwargs):
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
        tool = AppleScriptRunTool()
        call = ToolCall(
            id="t15",
            name="AppleScriptRun",
            arguments={"script": "delay 5", "timeout_seconds": 1},
        )
        result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "timed out" in result.content
    proc.kill.assert_called()


# ---------- Taxonomy ----------


def test_taxonomy_lists_gui_capabilities():
    from opencomputer.agent.consent.capability_taxonomy import F1_CAPABILITIES

    assert F1_CAPABILITIES["gui.point_click"] == ConsentTier.PER_ACTION
    assert F1_CAPABILITIES["gui.applescript_run"] == ConsentTier.PER_ACTION
