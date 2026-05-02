"""Groq provider tests.

Imports via the underscore alias (extensions.groq_provider) — the
hyphen→underscore aliasing is wired in tests/conftest.py.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from extensions.groq_provider.provider import GroqProvider

from plugin_sdk.core import Message


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    return GroqProvider()


def test_provider_name_is_class_attribute():
    """register() uses the class attribute as the provider name; must be 'groq'."""
    assert GroqProvider.name == "groq"


def test_default_model():
    assert GroqProvider.default_model == "llama-3.3-70b-versatile"


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqProvider()


def test_missing_api_key_error_includes_signup_url(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match=r"console\.groq\.com"):
        GroqProvider()


def test_empty_string_api_key_raises(monkeypatch):
    """An empty string for either env or explicit arg must NOT silently pass."""
    monkeypatch.setenv("GROQ_API_KEY", "")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqProvider()
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqProvider(api_key="")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqProvider(api_key="   ")


def test_api_key_env_attribute():
    assert GroqProvider._api_key_env == "GROQ_API_KEY"


def test_default_base_url(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    p = GroqProvider()
    assert p._base_url == "https://api.groq.com/openai/v1"


def test_explicit_api_key_bypasses_env(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    p = GroqProvider(api_key="explicit-key")
    assert p._api_key == "explicit-key"


@pytest.mark.asyncio
async def test_complete_returns_provider_response(provider):
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={
        "id": "cmpl-groq",
        "model": "llama-3.3-70b-versatile",
        "choices": [{"message": {"role": "assistant", "content": "fast answer"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    })
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)):
        resp = await provider.complete(
            model="llama-3.3-70b-versatile",
            messages=[Message(role="user", content="hello")],
        )
    assert resp.message.content == "fast answer"
    assert resp.message.role == "assistant"
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 3


@pytest.mark.asyncio
async def test_stream_complete_yields_text_delta_then_done(provider):
    """stream_complete MUST yield StreamEvent objects, finish with `done` event
    carrying the full ProviderResponse.
    """
    async def fake_lines():
        for line in [
            'data: {"choices":[{"delta":{"content":"qui"}}]}',
            'data: {"choices":[{"delta":{"content":"ck"}}]}',
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
        async for e in provider.stream_complete(
            model="llama-3.3-70b-versatile",
            messages=[Message(role="user", content="hi")],
        ):
            events.append(e)
    text_chunks = [e.text for e in events if e.kind == "text_delta"]
    assert "".join(text_chunks) == "quick"
    assert events[-1].kind == "done"
    assert events[-1].response is not None
    assert events[-1].response.message.content == "quick"


@pytest.mark.asyncio
async def test_stream_emits_done_event_even_on_http_error(provider):
    """Critical regression: try/finally guarantees the `done` sentinel fires
    even when the HTTP layer raises — agent loop hangs without this.
    """
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 401
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("401 Unauthorized", request=MagicMock(), response=mock_resp)
    )

    class _CM:
        async def __aenter__(self): return mock_resp  # noqa: N805
        async def __aexit__(self, *a): return None  # noqa: N805

    events = []
    raised = False
    with patch("httpx.AsyncClient.stream", MagicMock(return_value=_CM())):
        try:
            async for e in provider.stream_complete(
                model="llama-3.3-70b-versatile",
                messages=[Message(role="user", content="hi")],
            ):
                events.append(e)
        except httpx.HTTPStatusError:
            raised = True
    assert raised
    assert any(e.kind == "done" for e in events)


@pytest.mark.asyncio
async def test_stream_accumulates_tool_call_deltas(provider):
    """Critical regression: tool_calls stream as partial deltas; the final
    `done` event MUST carry assembled ToolCall objects.
    """
    async def fake_lines():
        for line in [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_x","function":{"name":"sum","arguments":"{\\"x\\":"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"5}"}}]}}]}',
            'data: {"choices":[{"finish_reason":"tool_calls"}]}',
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
        async for e in provider.stream_complete(
            model="llama-3.3-70b-versatile",
            messages=[Message(role="user", content="hi")],
        ):
            events.append(e)
    done = next(e for e in events if e.kind == "done")
    assert done.response.stop_reason == "tool_use"
    tool_calls = done.response.message.tool_calls
    assert tool_calls is not None and len(tool_calls) == 1
    assert tool_calls[0].id == "call_x"
    assert tool_calls[0].name == "sum"
    assert tool_calls[0].arguments == {"x": 5}
