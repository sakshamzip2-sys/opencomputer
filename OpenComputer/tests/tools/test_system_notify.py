"""Tests for SystemNotifyTool."""
from unittest.mock import patch

import pytest

from opencomputer.tools.system_notify import SystemNotifyTool
from plugin_sdk.core import ToolCall


@pytest.fixture
def tool():
    return SystemNotifyTool()


def _call(args: dict) -> ToolCall:
    return ToolCall(id="t1", name="SystemNotify", arguments=args)


def test_schema_shape(tool):
    s = tool.schema
    assert s.name == "SystemNotify"
    assert s.parameters["required"] == ["title"]


def test_capability_claim(tool):
    claims = tool.capability_claims
    assert claims[0].capability_id == "gui.system_notify"
    from plugin_sdk.consent import ConsentTier
    assert claims[0].tier_required == ConsentTier.EXPLICIT


def test_parallel_safe_true(tool):
    assert tool.parallel_safe is True


@pytest.mark.asyncio
async def test_requires_title(tool):
    result = await tool.execute(_call({}))
    assert result.is_error
    assert "title" in result.content.lower()


@pytest.mark.asyncio
async def test_title_too_long(tool):
    result = await tool.execute(_call({"title": "x" * 300}))
    assert result.is_error


@pytest.mark.asyncio
async def test_body_too_long(tool):
    result = await tool.execute(_call({"title": "ok", "body": "x" * 2000}))
    assert result.is_error


@pytest.mark.asyncio
async def test_invalid_urgency(tool):
    result = await tool.execute(_call({"title": "ok", "urgency": "extreme"}))
    assert result.is_error


@pytest.mark.asyncio
async def test_macos_dispatches_osascript(tool):
    with patch("opencomputer.tools.system_notify.detect_platform", return_value="macos"), \
         patch("opencomputer.tools.system_notify._notify_osascript", return_value=True) as m:
        result = await tool.execute(_call({"title": "Build done"}))
    assert not result.is_error
    m.assert_called_once()


@pytest.mark.asyncio
async def test_linux_dispatches_notify_send(tool):
    with patch("opencomputer.tools.system_notify.detect_platform", return_value="linux"), \
         patch("opencomputer.tools.system_notify._notify_send", return_value=True) as m:
        result = await tool.execute(_call({"title": "Build done", "body": "All tests passed"}))
    assert not result.is_error
    m.assert_called_once()


@pytest.mark.asyncio
async def test_windows_dispatches_powershell(tool):
    with patch("opencomputer.tools.system_notify.detect_platform", return_value="windows"), \
         patch("opencomputer.tools.system_notify._notify_powershell", return_value=True) as m:
        result = await tool.execute(_call({"title": "Build done"}))
    assert not result.is_error
    m.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_platform_returns_error(tool):
    with patch("opencomputer.tools.system_notify.detect_platform", return_value="unknown"):
        result = await tool.execute(_call({"title": "ok"}))
    assert result.is_error


@pytest.mark.asyncio
async def test_backend_error_returns_clean_error(tool):
    with patch("opencomputer.tools.system_notify.detect_platform", return_value="linux"), \
         patch("opencomputer.tools.system_notify._notify_send", return_value=False):
        result = await tool.execute(_call({"title": "ok"}))
    assert result.is_error
    assert "no backend" in result.content.lower()


def test_notify_send_threads_urgency():
    from opencomputer.tools.system_notify import _notify_send

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("opencomputer.tools.system_notify.has_command", return_value=True), \
         patch("subprocess.run", side_effect=fake_run):
        _notify_send("hello", "world", "critical")
    assert "--urgency" in captured["cmd"]
    assert "critical" in captured["cmd"]


def test_osascript_escapes_quotes_in_title():
    from opencomputer.tools.system_notify import _notify_osascript

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("opencomputer.tools.system_notify.has_command", return_value=True), \
         patch("subprocess.run", side_effect=fake_run):
        _notify_osascript('he said "hi"', "")
    script = captured["cmd"][2]
    assert '\\"' in script


def test_powershell_falls_back_to_balloon():
    from opencomputer.tools.system_notify import _notify_powershell

    captured = {}

    def fake_run(cmd, **kw):
        captured["script"] = cmd[-1]
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("opencomputer.tools.system_notify.has_command", return_value=True), \
         patch("subprocess.run", side_effect=fake_run):
        _notify_powershell("hello", "world")
    assert "BurntToast" in captured["script"]
    assert "BalloonTip" in captured["script"]
