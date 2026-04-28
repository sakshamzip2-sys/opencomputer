"""Tests for /fast, /usage, /platforms slash commands."""
import pytest

from opencomputer.agent.slash_commands_impl.fast_cmd import FastCommand
from opencomputer.agent.slash_commands_impl.platforms_cmd import PlatformsCommand
from opencomputer.agent.slash_commands_impl.usage_cmd import UsageCommand
from plugin_sdk.runtime_context import RuntimeContext


def _fresh_runtime(**custom) -> RuntimeContext:
    return RuntimeContext(custom=dict(custom))


# --- /fast ---


@pytest.mark.asyncio
async def test_fast_on():
    rt = _fresh_runtime()
    result = await FastCommand().execute("on", rt)
    assert "PRIORITY" in result.output.upper()
    assert rt.custom["service_tier"] == "priority"


@pytest.mark.asyncio
async def test_fast_off():
    rt = _fresh_runtime(service_tier="priority")
    result = await FastCommand().execute("off", rt)
    assert rt.custom["service_tier"] == "default"


@pytest.mark.asyncio
async def test_fast_alias_normal():
    rt = _fresh_runtime(service_tier="priority")
    result = await FastCommand().execute("normal", rt)
    assert rt.custom["service_tier"] == "default"


@pytest.mark.asyncio
async def test_fast_alias_fast():
    rt = _fresh_runtime()
    result = await FastCommand().execute("fast", rt)
    assert rt.custom["service_tier"] == "priority"


@pytest.mark.asyncio
async def test_fast_no_args_toggles():
    rt = _fresh_runtime()
    await FastCommand().execute("", rt)
    assert rt.custom["service_tier"] == "priority"
    await FastCommand().execute("", rt)
    assert rt.custom["service_tier"] == "default"


@pytest.mark.asyncio
async def test_fast_status_does_not_mutate():
    rt = _fresh_runtime(service_tier="priority")
    result = await FastCommand().execute("status", rt)
    assert "priority" in result.output
    assert rt.custom["service_tier"] == "priority"


@pytest.mark.asyncio
async def test_fast_invalid_arg_shows_usage():
    rt = _fresh_runtime()
    result = await FastCommand().execute("turbo", rt)
    assert "Usage" in result.output
    assert "service_tier" not in rt.custom


# --- /usage ---


@pytest.mark.asyncio
async def test_usage_with_no_data_shows_not_tracked():
    rt = _fresh_runtime()
    result = await UsageCommand().execute("", rt)
    assert "input tokens" in result.output
    assert "not tracked" in result.output.lower()


@pytest.mark.asyncio
async def test_usage_with_data_renders():
    rt = _fresh_runtime(
        session_tokens_in=12500,
        session_tokens_out=3200,
        session_cost_usd=0.42,
    )
    result = await UsageCommand().execute("", rt)
    assert "12.5K" in result.output or "12500" in result.output
    assert "3.2K" in result.output or "3200" in result.output
    assert "$0.42" in result.output


@pytest.mark.asyncio
async def test_usage_with_rate_limit_data():
    rt = _fresh_runtime(
        session_tokens_in=100,
        rate_limit_remaining=42,
        rate_limit_reset_at="2026-04-28T15:30:00Z",
    )
    result = await UsageCommand().execute("", rt)
    assert "Rate limit" in result.output
    assert "42" in result.output


@pytest.mark.asyncio
async def test_usage_handles_million_token_count():
    rt = _fresh_runtime(session_tokens_in=2_500_000)
    result = await UsageCommand().execute("", rt)
    assert "2.50M" in result.output or "2500000" in result.output


# --- /platforms ---


@pytest.mark.asyncio
async def test_platforms_empty_state():
    rt = _fresh_runtime()
    result = await PlatformsCommand().execute("", rt)
    assert "No active" in result.output
    assert "gateway" in result.output.lower()


@pytest.mark.asyncio
async def test_platforms_with_string_list():
    rt = _fresh_runtime(active_platforms=["telegram", "discord"])
    result = await PlatformsCommand().execute("", rt)
    assert "telegram" in result.output
    assert "discord" in result.output


@pytest.mark.asyncio
async def test_platforms_with_dict_list():
    rt = _fresh_runtime(active_platforms=[
        {"name": "telegram", "status": "polling"},
        {"name": "slack", "status": "connected"},
    ])
    result = await PlatformsCommand().execute("", rt)
    assert "telegram" in result.output
    assert "polling" in result.output
    assert "slack" in result.output


# --- metadata ---


def test_all_three_have_metadata():
    for cls in (FastCommand, UsageCommand, PlatformsCommand):
        cmd = cls()
        assert cmd.name
        assert cmd.description
