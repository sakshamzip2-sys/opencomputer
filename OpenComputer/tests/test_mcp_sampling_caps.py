"""Hermes parity G11: MCP sampling caps (max_tokens_cap, allowed_models)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def test_caps_dataclass_defaults():
    from opencomputer.agent.config import MCPSamplingCaps

    c = MCPSamplingCaps()
    assert c.max_tokens_cap == 4096
    assert c.max_rpm == 60
    assert c.max_tool_rounds == 5
    assert c.allowed_models == ()


def test_caps_dataclass_custom():
    from opencomputer.agent.config import MCPSamplingCaps

    c = MCPSamplingCaps(
        max_tokens_cap=128,
        max_rpm=10,
        max_tool_rounds=2,
        allowed_models=("anthropic/claude-opus", "openai/gpt-4o"),
    )
    assert c.max_tokens_cap == 128
    assert c.max_rpm == 10
    assert c.max_tool_rounds == 2
    assert c.allowed_models == ("anthropic/claude-opus", "openai/gpt-4o")


@pytest.mark.asyncio
async def test_max_tokens_cap_clips_request():
    from opencomputer.agent.config import MCPSamplingCaps
    from opencomputer.mcp.sampling import make_sampling_callback

    caps = MCPSamplingCaps(max_tokens_cap=128)
    cb = make_sampling_callback(caps=caps)

    seen_max_tokens = []

    async def fake_complete_text(messages, system, max_tokens, temperature):
        seen_max_tokens.append(max_tokens)
        return "ok"

    with patch(
        "opencomputer.mcp.sampling.complete_text",
        new=AsyncMock(side_effect=fake_complete_text),
    ):
        params = SimpleNamespace(
            messages=[
                SimpleNamespace(
                    role="user", content=SimpleNamespace(text="hi"),
                ),
            ],
            systemPrompt="",
            maxTokens=10000,
            temperature=1.0,
            modelPreferences=None,
        )
        await cb(None, params)
        assert seen_max_tokens[0] == 128, "request must be capped at max_tokens_cap"


@pytest.mark.asyncio
async def test_allowed_models_filter_rejects_non_listed():
    from opencomputer.agent.config import MCPSamplingCaps
    from opencomputer.mcp.sampling import make_sampling_callback

    caps = MCPSamplingCaps(allowed_models=("anthropic/claude-opus",))
    cb = make_sampling_callback(caps=caps)

    params = SimpleNamespace(
        messages=[
            SimpleNamespace(role="user", content=SimpleNamespace(text="hi")),
        ],
        systemPrompt="",
        maxTokens=100,
        temperature=1.0,
        modelPreferences=SimpleNamespace(
            hints=[SimpleNamespace(name="openai/gpt-4o")],
        ),
    )
    result = await cb(None, params)
    assert hasattr(result, "code"), (
        f"expected ErrorData but got {type(result).__name__}"
    )


@pytest.mark.asyncio
async def test_allowed_models_filter_passes_listed_model():
    from opencomputer.agent.config import MCPSamplingCaps
    from opencomputer.mcp.sampling import make_sampling_callback

    caps = MCPSamplingCaps(
        allowed_models=("anthropic/claude-opus", "openai/gpt-4o"),
    )
    cb = make_sampling_callback(caps=caps)

    async def fake_complete_text(messages, system, max_tokens, temperature):
        return "approved"

    with patch(
        "opencomputer.mcp.sampling.complete_text",
        new=AsyncMock(side_effect=fake_complete_text),
    ):
        params = SimpleNamespace(
            messages=[
                SimpleNamespace(role="user", content=SimpleNamespace(text="hi")),
            ],
            systemPrompt="",
            maxTokens=100,
            temperature=1.0,
            modelPreferences=SimpleNamespace(
                hints=[SimpleNamespace(name="openai/gpt-4o")],
            ),
        )
        result = await cb(None, params)
        assert hasattr(result, "content"), "model in allowlist should pass"


@pytest.mark.asyncio
async def test_no_caps_means_legacy_behavior():
    """Back-compat: callback works without caps (existing OC behavior)."""
    from opencomputer.mcp.sampling import make_sampling_callback

    cb = make_sampling_callback()

    async def fake_complete_text(messages, system, max_tokens, temperature):
        return "ok"

    with patch(
        "opencomputer.mcp.sampling.complete_text",
        new=AsyncMock(side_effect=fake_complete_text),
    ):
        params = SimpleNamespace(
            messages=[
                SimpleNamespace(role="user", content=SimpleNamespace(text="hi")),
            ],
            systemPrompt="",
            maxTokens=1024,
            temperature=1.0,
            modelPreferences=None,
        )
        result = await cb(None, params)
        assert hasattr(result, "content"), "no-caps path must succeed"
