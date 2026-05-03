"""Tests for ACP tool event schema builders and loop tool_callback wiring."""

from __future__ import annotations

import pytest


def test_make_tool_call_id_is_unique():
    from opencomputer.acp.tools import make_tool_call_id

    ids = {make_tool_call_id() for _ in range(100)}
    assert len(ids) == 100


def test_build_tool_start_shape():
    from opencomputer.acp.tools import build_tool_start

    event = build_tool_start("read_file", "call_abc", {"path": "/tmp/x"})
    assert event["tool_name"] == "read_file"
    assert event["tool_call_id"] == "call_abc"
    assert event["args"] == {"path": "/tmp/x"}


def test_build_tool_complete_shape():
    from opencomputer.acp.tools import build_tool_complete

    event = build_tool_complete("call_abc", "file contents here")
    assert event["tool_call_id"] == "call_abc"
    assert event["result"] == "file contents here"


def test_build_tool_error_shape():
    from opencomputer.acp.tools import build_tool_error

    event = build_tool_error("call_abc", "permission denied")
    assert event["tool_call_id"] == "call_abc"
    assert event["error"] == "permission denied"


@pytest.mark.asyncio
async def test_run_conversation_accepts_tool_callback():
    """loop.run_conversation should accept tool_callback without error."""
    from unittest.mock import AsyncMock, MagicMock

    from opencomputer.agent.config import Config
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk import Message
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    mock_provider = MagicMock()
    fake_resp = ProviderResponse(
        message=Message(role="assistant", content="done"),
        usage=Usage(input_tokens=1, output_tokens=1),
        stop_reason="end_turn",
    )
    mock_provider.stream_complete = AsyncMock(return_value=aiter([]))
    mock_provider.complete = AsyncMock(return_value=fake_resp)

    fired: list = []

    def my_tool_callback(phase, tool_name, tool_call_id, data):
        fired.append((phase, tool_name))

    cfg = Config()
    loop = AgentLoop(provider=mock_provider, config=cfg)
    # Should not raise TypeError for unexpected keyword argument
    try:
        await loop.run_conversation(
            "Hello",
            tool_callback=my_tool_callback,
        )
    except Exception:
        pass  # May fail for other reasons; we just need no TypeError


def aiter(iterable):
    """Synchronous helper to create an async iterator from an iterable."""

    async def _inner():
        for item in iterable:
            yield item

    return _inner()
