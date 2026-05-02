"""Ollama provider tests.

Imports via the underscore alias (extensions.ollama_provider) — the
hyphen→underscore aliasing is wired in tests/conftest.py.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from extensions.ollama_provider.provider import OllamaProvider

from plugin_sdk.core import Message


@pytest.fixture
def provider():
    return OllamaProvider(api_key=None, base_url="http://localhost:11434/v1")


def test_provider_name_is_class_attribute():
    """register() uses the class attribute as the provider name; must be 'ollama'."""
    assert OllamaProvider.name == "ollama"


def test_default_base_url_uses_local_ollama():
    p = OllamaProvider()
    assert p._base_url == "http://localhost:11434/v1"


def test_env_var_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://other:9999/v1")
    p = OllamaProvider()
    assert p._base_url == "http://other:9999/v1"


@pytest.mark.asyncio
async def test_complete_returns_provider_response(provider):
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={
        "id": "cmpl-x",
        "model": "llama3",
        "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    })
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)):
        resp = await provider.complete(model="llama3", messages=[Message(role="user", content="hi")])
    assert resp.message.content == "hello"
    assert resp.message.role == "assistant"
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 5
    assert resp.usage.output_tokens == 1


@pytest.mark.asyncio
async def test_stream_complete_yields_text_delta_then_done(provider):
    """Critical: stream_complete MUST yield StreamEvent objects (not the
    rev-1 fictional StreamDelta), and finish with a `done` event carrying
    the full ProviderResponse — that's what the agent loop unwraps.
    """
    async def fake_lines():
        for line in [
            'data: {"choices":[{"delta":{"content":"hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            'data: {"choices":[{"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]:
            yield line

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.aiter_lines = fake_lines

    class _CM:
        async def __aenter__(self): return mock_resp  # noqa: N805
        async def __aexit__(self, *a): return None  # noqa: N805

    with patch("httpx.AsyncClient.stream", MagicMock(return_value=_CM())):
        events = []
        async for e in provider.stream_complete(model="llama3", messages=[Message(role="user", content="hi")]):
            events.append(e)
    text_chunks = [e.text for e in events if e.kind == "text_delta"]
    assert "".join(text_chunks) == "hello"
    # Final event must be `done` with a complete ProviderResponse
    assert events[-1].kind == "done"
    assert events[-1].response is not None
    assert events[-1].response.message.content == "hello"
