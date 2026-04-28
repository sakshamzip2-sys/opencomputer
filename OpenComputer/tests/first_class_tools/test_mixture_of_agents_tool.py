"""Tests for MixtureOfAgentsTool — voting across N OpenRouter models."""
import json

import httpx
import pytest

from opencomputer.tools.mixture_of_agents import MixtureOfAgentsTool
from plugin_sdk.core import ToolCall


def _mock_completion(text: str) -> dict:
    """Shape mimicking OpenRouter / OpenAI Chat Completions API."""
    return {
        "id": "comp_x",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
    }


def _make_transport(responses: list[str]) -> httpx.MockTransport:
    """Cycle through the given responses, one per request."""
    counter = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        idx = counter["i"] % len(responses)
        counter["i"] += 1
        return httpx.Response(200, json=_mock_completion(responses[idx]))

    return httpx.MockTransport(handler)


@pytest.fixture
def tool():
    return MixtureOfAgentsTool(api_key="test-key")


@pytest.mark.asyncio
async def test_basic_voting_with_majority(tool, monkeypatch):
    """Three models, two return same answer — that wins."""
    transport = _make_transport(["The answer is 42", "The answer is 42", "Maybe 7"])
    monkeypatch.setattr(
        "opencomputer.tools.mixture_of_agents._make_async_client",
        lambda timeout=120.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    call = ToolCall(
        id="c1",
        name="MixtureOfAgents",
        arguments={
            "prompt": "What is the meaning of life?",
            "models": ["m1", "m2", "m3"],
        },
    )
    result = await tool.execute(call)
    assert not result.is_error
    # All 3 responses surfaced
    assert "The answer is 42" in result.content
    assert "Maybe 7" in result.content


@pytest.mark.asyncio
async def test_returns_all_responses(tool, monkeypatch):
    """Output must include all model responses for transparency."""
    transport = _make_transport(["Response A", "Response B"])
    monkeypatch.setattr(
        "opencomputer.tools.mixture_of_agents._make_async_client",
        lambda timeout=120.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    call = ToolCall(
        id="c2",
        name="MixtureOfAgents",
        arguments={
            "prompt": "test",
            "models": ["m1", "m2"],
        },
    )
    result = await tool.execute(call)
    assert "Response A" in result.content
    assert "Response B" in result.content


@pytest.mark.asyncio
async def test_missing_prompt_returns_error(tool):
    call = ToolCall(
        id="c3",
        name="MixtureOfAgents",
        arguments={"models": ["m1"]},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "prompt" in result.content.lower()


@pytest.mark.asyncio
async def test_empty_models_returns_error(tool):
    call = ToolCall(
        id="c4",
        name="MixtureOfAgents",
        arguments={"prompt": "x", "models": []},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "model" in result.content.lower()


@pytest.mark.asyncio
async def test_single_model_works(tool, monkeypatch):
    """MoA with one model is degenerate but should still work."""
    transport = _make_transport(["Single response"])
    monkeypatch.setattr(
        "opencomputer.tools.mixture_of_agents._make_async_client",
        lambda timeout=120.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    call = ToolCall(
        id="c5",
        name="MixtureOfAgents",
        arguments={"prompt": "x", "models": ["only-one"]},
    )
    result = await tool.execute(call)
    assert not result.is_error
    assert "Single response" in result.content


@pytest.mark.asyncio
async def test_no_api_key_returns_clear_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    tool = MixtureOfAgentsTool(api_key=None)
    call = ToolCall(
        id="c6",
        name="MixtureOfAgents",
        arguments={"prompt": "x", "models": ["m1"]},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "OPENROUTER_API_KEY" in result.content or "api key" in result.content.lower()


@pytest.mark.asyncio
async def test_partial_failure_includes_successful_responses(tool, monkeypatch):
    """If 1 of 3 models errors, the other 2 responses still surface."""
    counter = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        idx = counter["i"]
        counter["i"] += 1
        if idx == 1:
            return httpx.Response(500, json={"detail": "model unavailable"})
        return httpx.Response(200, json=_mock_completion(f"Response {idx}"))

    monkeypatch.setattr(
        "opencomputer.tools.mixture_of_agents._make_async_client",
        lambda timeout=120.0: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout),
    )
    call = ToolCall(
        id="c7",
        name="MixtureOfAgents",
        arguments={"prompt": "x", "models": ["m1", "m2", "m3"]},
    )
    result = await tool.execute(call)
    # Should not be a hard error — partial success is normal
    assert "Response 0" in result.content
    assert "Response 2" in result.content
    # The failure should be noted
    assert "m2" in result.content and ("error" in result.content.lower() or "failed" in result.content.lower())


def test_schema_shape(tool):
    s = tool.schema
    assert s.name == "MixtureOfAgents"
    props = s.parameters["properties"]
    assert "prompt" in props
    assert "models" in props
    required = s.parameters.get("required", [])
    assert "prompt" in required
    assert "models" in required


@pytest.mark.asyncio
async def test_max_models_cap(tool):
    """Bounded so a model can't accidentally invoke 100 calls."""
    call = ToolCall(
        id="c8",
        name="MixtureOfAgents",
        arguments={
            "prompt": "x",
            "models": [f"model-{i}" for i in range(20)],
        },
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "too many" in result.content.lower() or "limit" in result.content.lower()
