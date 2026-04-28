"""Tests for SystemKeystrokeTool."""
from unittest.mock import patch

import pytest

from opencomputer.tools.system_keystroke import SystemKeystrokeTool
from plugin_sdk.core import ToolCall


@pytest.fixture
def tool():
    return SystemKeystrokeTool()


def _call(args: dict) -> ToolCall:
    return ToolCall(id="t1", name="SystemKeystroke", arguments=args)


def test_schema_shape(tool):
    s = tool.schema
    assert s.name == "SystemKeystroke"
    assert "text" in s.parameters["properties"]
    assert "hotkey" in s.parameters["properties"]


def test_capability_claim(tool):
    claims = tool.capability_claims
    assert claims[0].capability_id == "gui.system_keystroke"


@pytest.mark.asyncio
async def test_requires_text_or_hotkey(tool):
    result = await tool.execute(_call({}))
    assert result.is_error
    assert "text or hotkey" in result.content.lower()


@pytest.mark.asyncio
async def test_text_and_hotkey_mutually_exclusive(tool):
    result = await tool.execute(_call({"text": "hi", "hotkey": "ctrl,c"}))
    assert result.is_error
    assert "mutually exclusive" in result.content.lower()


@pytest.mark.asyncio
async def test_text_too_long(tool):
    result = await tool.execute(_call({"text": "x" * 5000}))
    assert result.is_error
    assert "cap" in result.content.lower()


@pytest.mark.asyncio
async def test_empty_hotkey_after_split(tool):
    result = await tool.execute(_call({"hotkey": ",,,"}))
    assert result.is_error
    assert "empty" in result.content.lower()


@pytest.mark.asyncio
async def test_text_macos_pyautogui(tool):
    with patch("opencomputer.tools.system_keystroke.detect_platform", return_value="macos"), \
         patch("opencomputer.tools.system_keystroke._type_pyautogui", return_value=True), \
         patch("opencomputer.tools.system_keystroke._type_osascript", return_value=False):
        result = await tool.execute(_call({"text": "hello"}))
    assert not result.is_error
    assert "5 chars" in result.content


@pytest.mark.asyncio
async def test_text_macos_falls_back_osascript(tool):
    with patch("opencomputer.tools.system_keystroke.detect_platform", return_value="macos"), \
         patch("opencomputer.tools.system_keystroke._type_pyautogui", return_value=False), \
         patch("opencomputer.tools.system_keystroke._type_osascript", return_value=True) as m_o:
        result = await tool.execute(_call({"text": "hello"}))
    assert not result.is_error
    m_o.assert_called_once()


@pytest.mark.asyncio
async def test_hotkey_dispatch(tool):
    captured = {}

    def capture(platform, keys):
        captured["keys"] = keys
        return True

    with patch("opencomputer.tools.system_keystroke.detect_platform", return_value="linux"), \
         patch("opencomputer.tools.system_keystroke._hotkey_dispatch", side_effect=capture):
        result = await tool.execute(_call({"hotkey": "ctrl,c"}))
    assert not result.is_error
    assert captured["keys"] == ["ctrl", "c"]
    assert "ctrl+c" in result.content


@pytest.mark.asyncio
async def test_hotkey_with_spaces_trimmed(tool):
    captured = {}

    def capture(platform, keys):
        captured["keys"] = keys
        return True

    with patch("opencomputer.tools.system_keystroke._hotkey_dispatch", side_effect=capture):
        await tool.execute(_call({"hotkey": "ctrl , shift , a"}))
    assert captured["keys"] == ["ctrl", "shift", "a"]


@pytest.mark.asyncio
async def test_no_backend(tool):
    with patch("opencomputer.tools.system_keystroke.detect_platform", return_value="windows"), \
         patch("opencomputer.tools.system_keystroke._type_pyautogui", return_value=False):
        result = await tool.execute(_call({"text": "x"}))
    assert result.is_error
    assert "no backend" in result.content.lower()


def test_hotkey_xdotool_combo_format():
    from opencomputer.tools.system_keystroke import _hotkey_xdotool

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("opencomputer.tools.system_keystroke.detect_linux_display_server", return_value="x11"), \
         patch("opencomputer.tools.system_keystroke.has_command", return_value=True), \
         patch("subprocess.run", side_effect=fake_run):
        ok = _hotkey_xdotool(["ctrl", "c"])
    assert ok
    assert captured["cmd"] == ["xdotool", "key", "ctrl+c"]


def test_osascript_text_escapes_quotes():
    from opencomputer.tools.system_keystroke import _type_osascript

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("opencomputer.tools.system_keystroke.has_command", return_value=True), \
         patch("subprocess.run", side_effect=fake_run):
        _type_osascript('say "hi"')
    script_arg = captured["cmd"][2]
    assert '\\"' in script_arg
