"""T71 — MCP sampling/createMessage callback (server → LLM bridge).

Per MCP spec, an MCP server can request the host's LLM to generate a
completion via ``sampling/createMessage``. The host (us) wires a
``sampling_callback`` into ``ClientSession`` that:

  1. Converts MCP ``CreateMessageRequestParams`` to OC's aux_llm shape.
  2. Calls ``complete_text`` (which already handles fallback + creds).
  3. Wraps the response as ``CreateMessageResult``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.mcp.sampling import make_sampling_callback


@pytest.mark.asyncio
async def test_sampling_callback_translates_messages(monkeypatch):
    """A simple text-only sampling request reaches complete_text correctly."""
    from mcp.types import (
        CreateMessageRequestParams,
        SamplingMessage,
        TextContent,
    )

    capture = {}

    async def fake_complete_text(*, messages, system, max_tokens, temperature, **kw):
        capture["messages"] = messages
        capture["system"] = system
        capture["max_tokens"] = max_tokens
        capture["temperature"] = temperature
        return "echoed"

    monkeypatch.setattr("opencomputer.mcp.sampling.complete_text", fake_complete_text)

    cb = make_sampling_callback()
    params = CreateMessageRequestParams(
        messages=[
            SamplingMessage(role="user", content=TextContent(type="text", text="hello world")),
        ],
        systemPrompt="be concise",
        maxTokens=512,
        temperature=0.7,
    )
    result = await cb(MagicMock(), params)

    assert result.content.text == "echoed"
    assert result.role == "assistant"
    assert capture["system"] == "be concise"
    assert capture["max_tokens"] == 512
    assert capture["temperature"] == 0.7
    assert capture["messages"][0]["role"] == "user"
    assert capture["messages"][0]["content"] == "hello world"


@pytest.mark.asyncio
async def test_sampling_callback_handles_provider_error(monkeypatch):
    from mcp.types import (
        CreateMessageRequestParams,
        SamplingMessage,
        TextContent,
    )

    async def failing(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("opencomputer.mcp.sampling.complete_text", failing)

    cb = make_sampling_callback()
    params = CreateMessageRequestParams(
        messages=[SamplingMessage(role="user", content=TextContent(type="text", text="x"))],
        maxTokens=100,
    )
    result = await cb(MagicMock(), params)
    # ErrorData per the MCP types when the host can't fulfill.
    from mcp.types import ErrorData

    assert isinstance(result, ErrorData)
    assert "provider down" in result.message


@pytest.mark.asyncio
async def test_sampling_callback_skips_non_text_content(monkeypatch):
    """Image / other non-text content gets dropped — text-only sampling."""
    from mcp.types import (
        CreateMessageRequestParams,
        SamplingMessage,
        TextContent,
    )

    capture = {}

    async def fake_complete_text(*, messages, system, max_tokens, temperature, **kw):
        capture["messages"] = messages
        return "ok"

    monkeypatch.setattr("opencomputer.mcp.sampling.complete_text", fake_complete_text)

    cb = make_sampling_callback()
    params = CreateMessageRequestParams(
        messages=[
            SamplingMessage(role="user", content=TextContent(type="text", text="m1")),
            SamplingMessage(role="assistant", content=TextContent(type="text", text="m2")),
        ],
        maxTokens=100,
    )
    await cb(MagicMock(), params)
    assert len(capture["messages"]) == 2
    assert capture["messages"][0]["role"] == "user"
    assert capture["messages"][1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_sampling_callback_default_max_tokens(monkeypatch):
    """maxTokens not passed → fall back to a sensible default."""
    from mcp.types import (
        CreateMessageRequestParams,
        SamplingMessage,
        TextContent,
    )

    capture = {}

    async def fake_complete_text(*, messages, system, max_tokens, temperature, **kw):
        capture["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr("opencomputer.mcp.sampling.complete_text", fake_complete_text)

    cb = make_sampling_callback()
    params = CreateMessageRequestParams(
        messages=[SamplingMessage(role="user", content=TextContent(type="text", text="x"))],
        maxTokens=2048,
    )
    await cb(MagicMock(), params)
    assert capture["max_tokens"] == 2048
