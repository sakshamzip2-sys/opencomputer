"""Tests for /yolo slash command."""
import pytest

from opencomputer.agent.slash_commands_impl.yolo_cmd import YoloCommand
from plugin_sdk.runtime_context import RuntimeContext


def _fresh_runtime(yolo: bool = False) -> RuntimeContext:
    """Build a fresh runtime with a mutable custom dict."""
    return RuntimeContext(custom={"yolo_session": yolo} if yolo else {})


@pytest.mark.asyncio
async def test_yolo_on():
    rt = _fresh_runtime()
    cmd = YoloCommand()
    result = await cmd.execute("on", rt)
    assert "ON" in result.output
    assert rt.custom["yolo_session"] is True


@pytest.mark.asyncio
async def test_yolo_off():
    rt = _fresh_runtime(yolo=True)
    cmd = YoloCommand()
    result = await cmd.execute("off", rt)
    assert "OFF" in result.output
    assert rt.custom["yolo_session"] is False


@pytest.mark.asyncio
async def test_yolo_no_args_toggles_from_off_to_on():
    rt = _fresh_runtime()
    cmd = YoloCommand()
    result = await cmd.execute("", rt)
    assert "ON" in result.output
    assert rt.custom["yolo_session"] is True


@pytest.mark.asyncio
async def test_yolo_no_args_toggles_from_on_to_off():
    rt = _fresh_runtime(yolo=True)
    cmd = YoloCommand()
    result = await cmd.execute("", rt)
    assert "OFF" in result.output
    assert rt.custom["yolo_session"] is False


@pytest.mark.asyncio
async def test_yolo_status_when_on():
    rt = _fresh_runtime(yolo=True)
    cmd = YoloCommand()
    result = await cmd.execute("status", rt)
    assert "ON" in result.output
    # Status doesn't mutate
    assert rt.custom["yolo_session"] is True


@pytest.mark.asyncio
async def test_yolo_status_when_off():
    rt = _fresh_runtime()
    cmd = YoloCommand()
    result = await cmd.execute("status", rt)
    assert "OFF" in result.output


@pytest.mark.asyncio
async def test_yolo_invalid_arg_shows_usage():
    rt = _fresh_runtime()
    cmd = YoloCommand()
    result = await cmd.execute("yes", rt)
    assert "Usage" in result.output
    # Invalid args don't mutate
    assert "yolo_session" not in rt.custom or rt.custom["yolo_session"] is False


@pytest.mark.asyncio
async def test_yolo_on_warning_visible():
    """The ON message must include the warning so the user is aware."""
    rt = _fresh_runtime()
    cmd = YoloCommand()
    result = await cmd.execute("on", rt)
    assert "⚠" in result.output or "WARNING" in result.output.upper() or "skip" in result.output.lower()


def test_command_metadata():
    cmd = YoloCommand()
    assert cmd.name == "yolo"
    assert "yolo" in cmd.description.lower() or "approval" in cmd.description.lower()
