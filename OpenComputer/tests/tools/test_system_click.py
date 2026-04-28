"""Tests for SystemClickTool — cross-platform GUI click."""
from unittest.mock import patch

import pytest

from opencomputer.tools.system_click import SystemClickTool
from plugin_sdk.core import ToolCall


@pytest.fixture
def tool():
    return SystemClickTool()


def _call(args: dict) -> ToolCall:
    return ToolCall(id="t1", name="SystemClick", arguments=args)


def test_schema_shape(tool):
    s = tool.schema
    assert s.name == "SystemClick"
    assert "x" in s.parameters["properties"]
    assert "y" in s.parameters["properties"]
    assert s.parameters["required"] == ["x", "y"]


def test_capability_claim_shape(tool):
    claims = tool.capability_claims
    assert len(claims) == 1
    assert claims[0].capability_id == "gui.system_click"


def test_parallel_safe_false(tool):
    assert tool.parallel_safe is False


@pytest.mark.asyncio
async def test_validates_non_int(tool):
    result = await tool.execute(_call({"x": "100", "y": 0}))
    assert result.is_error
    assert "integer" in result.content.lower()


@pytest.mark.asyncio
async def test_validates_negative(tool):
    result = await tool.execute(_call({"x": -1, "y": 0}))
    assert result.is_error
    assert "out of range" in result.content.lower()


@pytest.mark.asyncio
async def test_validates_too_large(tool):
    result = await tool.execute(_call({"x": 99999, "y": 0}))
    assert result.is_error


@pytest.mark.asyncio
async def test_validates_invalid_button(tool):
    result = await tool.execute(_call({"x": 1, "y": 1, "button": "middle"}))
    assert result.is_error
    assert "button" in result.content.lower()


@pytest.mark.asyncio
async def test_macos_uses_quartz_when_available(tool):
    with patch("opencomputer.tools.system_click.detect_platform", return_value="macos"), \
         patch("opencomputer.tools.system_click._click_quartz", return_value=True) as m_q, \
         patch("opencomputer.tools.system_click._click_pyautogui", return_value=False), \
         patch("opencomputer.tools.system_click._click_osascript", return_value=False):
        result = await tool.execute(_call({"x": 100, "y": 200}))
    assert not result.is_error
    m_q.assert_called_once()


@pytest.mark.asyncio
async def test_macos_falls_back_to_pyautogui(tool):
    with patch("opencomputer.tools.system_click.detect_platform", return_value="macos"), \
         patch("opencomputer.tools.system_click._click_quartz", return_value=False), \
         patch("opencomputer.tools.system_click._click_pyautogui", return_value=True) as m_p, \
         patch("opencomputer.tools.system_click._click_osascript", return_value=False):
        result = await tool.execute(_call({"x": 100, "y": 200}))
    assert not result.is_error
    m_p.assert_called_once()


@pytest.mark.asyncio
async def test_macos_final_fallback_osascript(tool):
    with patch("opencomputer.tools.system_click.detect_platform", return_value="macos"), \
         patch("opencomputer.tools.system_click._click_quartz", return_value=False), \
         patch("opencomputer.tools.system_click._click_pyautogui", return_value=False), \
         patch("opencomputer.tools.system_click._click_osascript", return_value=True) as m_o:
        result = await tool.execute(_call({"x": 100, "y": 200}))
    assert not result.is_error
    m_o.assert_called_once()


@pytest.mark.asyncio
async def test_linux_uses_pyautogui_when_available(tool):
    with patch("opencomputer.tools.system_click.detect_platform", return_value="linux"), \
         patch("opencomputer.tools.system_click._click_pyautogui", return_value=True) as m_p, \
         patch("opencomputer.tools.system_click._click_xdotool", return_value=False):
        result = await tool.execute(_call({"x": 50, "y": 60}))
    assert not result.is_error
    m_p.assert_called_once()


@pytest.mark.asyncio
async def test_linux_falls_back_to_xdotool(tool):
    with patch("opencomputer.tools.system_click.detect_platform", return_value="linux"), \
         patch("opencomputer.tools.system_click._click_pyautogui", return_value=False), \
         patch("opencomputer.tools.system_click._click_xdotool", return_value=True) as m_x:
        result = await tool.execute(_call({"x": 50, "y": 60}))
    assert not result.is_error
    m_x.assert_called_once()


@pytest.mark.asyncio
async def test_windows_uses_pyautogui(tool):
    with patch("opencomputer.tools.system_click.detect_platform", return_value="windows"), \
         patch("opencomputer.tools.system_click._click_pyautogui", return_value=True):
        result = await tool.execute(_call({"x": 50, "y": 60}))
    assert not result.is_error


@pytest.mark.asyncio
async def test_windows_no_backend_returns_error(tool):
    with patch("opencomputer.tools.system_click.detect_platform", return_value="windows"), \
         patch("opencomputer.tools.system_click._click_pyautogui", return_value=False):
        result = await tool.execute(_call({"x": 50, "y": 60}))
    assert result.is_error
    assert "no backend" in result.content.lower()


@pytest.mark.asyncio
async def test_unknown_platform_returns_error(tool):
    with patch("opencomputer.tools.system_click.detect_platform", return_value="unknown"):
        result = await tool.execute(_call({"x": 1, "y": 1}))
    assert result.is_error


@pytest.mark.asyncio
async def test_button_right_threaded_through(tool):
    captured = {}

    def capture(x, y, button, double):
        captured["button"] = button
        return True

    with patch("opencomputer.tools.system_click.detect_platform", return_value="macos"), \
         patch("opencomputer.tools.system_click._click_quartz", side_effect=capture):
        await tool.execute(_call({"x": 1, "y": 2, "button": "right"}))
    assert captured["button"] == "right"


@pytest.mark.asyncio
async def test_double_click_threaded_through(tool):
    captured = {}

    def capture(x, y, button, double):
        captured["double"] = double
        return True

    with patch("opencomputer.tools.system_click.detect_platform", return_value="macos"), \
         patch("opencomputer.tools.system_click._click_quartz", side_effect=capture):
        await tool.execute(_call({"x": 1, "y": 2, "double": True}))
    assert captured["double"] is True


@pytest.mark.asyncio
async def test_backend_exception_caught(tool):
    def boom(*a, **kw):
        raise RuntimeError("display unavailable")

    with patch("opencomputer.tools.system_click.detect_platform", return_value="linux"), \
         patch("opencomputer.tools.system_click._click_dispatch", side_effect=boom):
        result = await tool.execute(_call({"x": 1, "y": 1}))
    assert result.is_error
    assert "click failed" in result.content.lower()


def test_xdotool_wayland_uses_ydotool():
    from opencomputer.tools.system_click import _click_xdotool

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("opencomputer.tools.system_click.detect_linux_display_server", return_value="wayland"), \
         patch("opencomputer.tools.system_click.has_command", return_value=True), \
         patch("subprocess.run", side_effect=fake_run):
        ok = _click_xdotool(10, 20, "left", False)
    assert ok
    assert captured["cmd"][0] == "ydotool"


def test_xdotool_x11_uses_xdotool():
    from opencomputer.tools.system_click import _click_xdotool

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("opencomputer.tools.system_click.detect_linux_display_server", return_value="x11"), \
         patch("opencomputer.tools.system_click.has_command", return_value=True), \
         patch("subprocess.run", side_effect=fake_run):
        ok = _click_xdotool(10, 20, "left", False)
    assert ok
    assert captured["cmd"][0] == "xdotool"


def test_osascript_refuses_right_click():
    from opencomputer.tools.system_click import _click_osascript

    with patch("opencomputer.tools.system_click.has_command", return_value=True):
        ok = _click_osascript(10, 20, "right", False)
    assert ok is False
