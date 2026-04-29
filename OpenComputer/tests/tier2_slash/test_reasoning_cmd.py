"""Tests for /reasoning slash command."""
import pytest

from opencomputer.agent.slash_commands_impl.reasoning_cmd import ReasoningCommand
from plugin_sdk.runtime_context import RuntimeContext


def _fresh_runtime(**custom) -> RuntimeContext:
    return RuntimeContext(custom=dict(custom))


@pytest.mark.asyncio
async def test_no_args_shows_default_status():
    rt = _fresh_runtime()
    cmd = ReasoningCommand()
    result = await cmd.execute("", rt)
    assert "medium" in result.output  # default level
    assert "hidden" in result.output


@pytest.mark.asyncio
async def test_set_level_high():
    rt = _fresh_runtime()
    cmd = ReasoningCommand()
    result = await cmd.execute("high", rt)
    assert "high" in result.output
    assert rt.custom["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_set_level_xhigh():
    rt = _fresh_runtime()
    cmd = ReasoningCommand()
    result = await cmd.execute("xhigh", rt)
    assert rt.custom["reasoning_effort"] == "xhigh"


@pytest.mark.asyncio
async def test_set_level_none_disables_reasoning():
    rt = _fresh_runtime()
    cmd = ReasoningCommand()
    result = await cmd.execute("none", rt)
    assert rt.custom["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_show_toggle():
    rt = _fresh_runtime()
    cmd = ReasoningCommand()
    result = await cmd.execute("show", rt)
    assert rt.custom["show_reasoning"] is True
    assert "shown" in result.output.lower() or "SHOWN" in result.output


@pytest.mark.asyncio
async def test_hide_toggle():
    rt = _fresh_runtime(show_reasoning=True)
    cmd = ReasoningCommand()
    result = await cmd.execute("hide", rt)
    assert rt.custom["show_reasoning"] is False
    assert "hidden" in result.output.lower() or "HIDDEN" in result.output


@pytest.mark.asyncio
async def test_invalid_level_shows_usage():
    rt = _fresh_runtime()
    cmd = ReasoningCommand()
    result = await cmd.execute("ultra", rt)
    assert "Usage" in result.output
    assert "reasoning_effort" not in rt.custom or rt.custom.get("reasoning_effort") != "ultra"


@pytest.mark.asyncio
async def test_status_subcommand_does_not_mutate():
    rt = _fresh_runtime(reasoning_effort="high", show_reasoning=True)
    cmd = ReasoningCommand()
    result = await cmd.execute("status", rt)
    assert "high" in result.output
    assert rt.custom["reasoning_effort"] == "high"
    assert rt.custom["show_reasoning"] is True


@pytest.mark.asyncio
async def test_set_level_then_status_reflects():
    rt = _fresh_runtime()
    cmd = ReasoningCommand()
    await cmd.execute("low", rt)
    result = await cmd.execute("status", rt)
    assert "low" in result.output


@pytest.mark.asyncio
async def test_show_and_set_level_independently():
    rt = _fresh_runtime()
    cmd = ReasoningCommand()
    await cmd.execute("show", rt)
    await cmd.execute("high", rt)
    # Both should be set
    assert rt.custom["show_reasoning"] is True
    assert rt.custom["reasoning_effort"] == "high"


def test_command_metadata():
    cmd = ReasoningCommand()
    assert cmd.name == "reasoning"
    assert "reasoning" in cmd.description.lower() or "thinking" in cmd.description.lower()
