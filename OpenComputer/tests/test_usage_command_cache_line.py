"""/usage shows a cache line when cache tokens are non-zero, omits otherwise."""

import pytest

from opencomputer.agent.slash_commands_impl.usage_cmd import UsageCommand
from plugin_sdk.runtime_context import RuntimeContext


@pytest.mark.asyncio
async def test_usage_renders_cache_line_when_present():
    rt = RuntimeContext(
        custom={
            "session_tokens_in": 1000,
            "session_tokens_out": 500,
            "session_cache_read": 12_400,
            "session_cache_write": 880,
        }
    )
    out = (await UsageCommand().execute("", rt)).output
    assert "cache" in out.lower()
    assert "12.4K" in out  # 12_400 → 12.4K
    assert "880" in out


@pytest.mark.asyncio
async def test_usage_omits_cache_line_when_zero():
    rt = RuntimeContext(
        custom={
            "session_tokens_in": 1000,
            "session_tokens_out": 500,
            "session_cache_read": 0,
            "session_cache_write": 0,
        }
    )
    out = (await UsageCommand().execute("", rt)).output
    assert "cache" not in out.lower()


@pytest.mark.asyncio
async def test_usage_omits_cache_line_when_keys_missing():
    """Old sessions / non-caching providers — keys never set."""
    rt = RuntimeContext(custom={})
    out = (await UsageCommand().execute("", rt)).output
    assert "cache" not in out.lower()


@pytest.mark.asyncio
async def test_usage_renders_when_only_one_side_present():
    """OpenAI-only sessions: read tokens only, no write tracking."""
    rt = RuntimeContext(custom={"session_cache_read": 500, "session_cache_write": 0})
    out = (await UsageCommand().execute("", rt)).output
    assert "cache" in out.lower()
    assert "500" in out
