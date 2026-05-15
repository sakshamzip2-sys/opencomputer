"""Tests for OpenAI Chat Completions request parameter compatibility."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Message

_PROVIDER_PATH = (
    Path(__file__).resolve().parents[1]
    / "extensions" / "openai-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_openai_provider_params", _PROVIDER_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_response():
    fake_choice = MagicMock()
    fake_choice.message = MagicMock(content="ok", tool_calls=None)
    fake_choice.finish_reason = "stop"
    fake_resp = MagicMock()
    fake_resp.choices = [fake_choice]
    fake_resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
    fake_resp.usage.prompt_tokens_details = None
    return fake_resp


@pytest.mark.asyncio
async def test_gpt5_uses_max_completion_tokens_not_max_tokens(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    mod = _load_provider_module()
    provider = mod.OpenAIProvider()
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_fake_response())
    provider.client = mock_client

    await provider.complete(
        model="gpt-5.4",
        messages=[Message(role="user", content="hi")],
        max_tokens=123,
    )

    kwargs = mock_client.chat.completions.create.await_args.kwargs
    assert kwargs["max_completion_tokens"] == 123
    assert "max_tokens" not in kwargs


@pytest.mark.asyncio
async def test_gpt4o_uses_max_completion_tokens_not_max_tokens(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    mod = _load_provider_module()
    provider = mod.OpenAIProvider()
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_fake_response())
    provider.client = mock_client

    await provider.complete(
        model="gpt-4o-mini",
        messages=[Message(role="user", content="hi")],
        max_tokens=321,
    )

    kwargs = mock_client.chat.completions.create.await_args.kwargs
    assert kwargs["max_completion_tokens"] == 321
    assert "max_tokens" not in kwargs


@pytest.mark.asyncio
async def test_openrouter_compatible_provider_keeps_max_tokens(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    mod = _load_provider_module()
    provider = mod.OpenAIProvider()
    provider.name = "openrouter"
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_fake_response())
    provider.client = mock_client

    await provider.complete(
        model="openai/gpt-5",
        messages=[Message(role="user", content="hi")],
        max_tokens=128,
    )

    kwargs = mock_client.chat.completions.create.await_args.kwargs
    assert kwargs["max_tokens"] == 128
    assert "max_completion_tokens" not in kwargs


@pytest.mark.asyncio
async def test_openrouter_caps_default_max_tokens(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_MAX_TOKENS", raising=False)
    mod = _load_provider_module()
    provider = mod.OpenAIProvider()
    provider.name = "openrouter"
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_fake_response())
    provider.client = mock_client

    await provider.complete(
        model="qwen/qwen3.5-35b-a3b",
        messages=[Message(role="user", content="hello")],
        max_tokens=32768,
    )

    kwargs = mock_client.chat.completions.create.await_args.kwargs
    assert kwargs["max_tokens"] == 256


@pytest.mark.asyncio
async def test_openrouter_max_tokens_cap_can_be_overridden(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MAX_TOKENS", "594")
    mod = _load_provider_module()
    provider = mod.OpenAIProvider()
    provider.name = "openrouter"
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_fake_response())
    provider.client = mock_client

    await provider.complete(
        model="qwen/qwen3.5-35b-a3b",
        messages=[Message(role="user", content="hello")],
        max_tokens=32768,
    )

    kwargs = mock_client.chat.completions.create.await_args.kwargs
    assert kwargs["max_tokens"] == 594
