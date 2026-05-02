"""Tests for opencomputer.agent.aux_llm — provider-agnostic LLM helpers.

These tests verify that the auxiliary LLM helpers route through
``plugin_registry.providers[<configured>]`` rather than importing
Anthropic directly. Without this routing, users with only an OpenAI /
Groq / Ollama key would lose access to /btw, vision_analyze, profile
bootstrap, etc.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.agent import aux_llm


@pytest.mark.asyncio
async def test_complete_text_routes_through_configured_provider(monkeypatch):
    """The helper resolves provider from cfg.model.provider, NOT
    hardcoded Anthropic. Mock the configured provider plugin and verify
    it receives the call with the right shape.
    """
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(
        return_value=MagicMock(
            message=MagicMock(content="provider-routed answer"),
        ),
    )
    fake_registry = MagicMock()
    fake_registry.providers = {"openai": fake_provider}

    fake_cfg = MagicMock()
    fake_cfg.model.provider = "openai"
    fake_cfg.model.name = "gpt-5.4"

    with patch.object(aux_llm, "_resolve_provider", return_value=fake_provider), \
         patch.object(aux_llm, "_resolve_default_model", return_value="gpt-5.4"):
        result = await aux_llm.complete_text(
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=42,
        )

    assert result == "provider-routed answer"
    fake_provider.complete.assert_called_once()
    kwargs = fake_provider.complete.call_args.kwargs
    assert kwargs["model"] == "gpt-5.4"
    assert kwargs["max_tokens"] == 42
    # Single message converted to plugin_sdk.core.Message
    assert len(kwargs["messages"]) == 1
    assert kwargs["messages"][0].role == "user"
    assert kwargs["messages"][0].content == "hi"


@pytest.mark.asyncio
async def test_complete_text_passes_system_kwarg(monkeypatch):
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(
        return_value=MagicMock(message=MagicMock(content="ok")),
    )
    with patch.object(aux_llm, "_resolve_provider", return_value=fake_provider), \
         patch.object(aux_llm, "_resolve_default_model", return_value="m"):
        await aux_llm.complete_text(
            messages=[{"role": "user", "content": "x"}],
            system="be brief",
        )
    assert fake_provider.complete.call_args.kwargs["system"] == "be brief"


@pytest.mark.asyncio
async def test_complete_text_returns_empty_string_when_provider_returns_no_message():
    """Defensive: a provider that returns ``response=None`` or empty must
    surface as empty string, not crash.
    """
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(
        return_value=MagicMock(message=None),
    )
    with patch.object(aux_llm, "_resolve_provider", return_value=fake_provider), \
         patch.object(aux_llm, "_resolve_default_model", return_value="m"):
        result = await aux_llm.complete_text(
            messages=[{"role": "user", "content": "x"}],
        )
    assert result == ""


@pytest.mark.asyncio
async def test_complete_vision_passes_image_content_array():
    """Vision call must format content as the multimodal array (image
    block + text block) that anthropic / openai-compat / gemini expect.
    """
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(
        return_value=MagicMock(message=MagicMock(content="a cat")),
    )
    with patch.object(aux_llm, "_resolve_provider", return_value=fake_provider), \
         patch.object(aux_llm, "_resolve_default_model", return_value="m"):
        result = await aux_llm.complete_vision(
            image_base64="base64data",
            mime_type="image/png",
            prompt="What is this?",
        )

    assert result == "a cat"
    sent_messages = fake_provider.complete.call_args.kwargs["messages"]
    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert msg.role == "user"
    # content is a list of blocks: image first, then text
    assert isinstance(msg.content, list)
    assert msg.content[0]["type"] == "image"
    assert msg.content[0]["source"]["data"] == "base64data"
    assert msg.content[0]["source"]["media_type"] == "image/png"
    assert msg.content[1]["type"] == "text"
    assert msg.content[1]["text"] == "What is this?"


def test_complete_text_sync_drives_async_under_the_hood(monkeypatch):
    """The sync wrapper exists so profile bootstrap (sync code path) can
    still use the helper. It must drive the async function with
    asyncio.run — same pattern as title_generator.call_llm.
    """
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(
        return_value=MagicMock(message=MagicMock(content="sync result")),
    )
    with patch.object(aux_llm, "_resolve_provider", return_value=fake_provider), \
         patch.object(aux_llm, "_resolve_default_model", return_value="m"):
        result = aux_llm.complete_text_sync(
            messages=[{"role": "user", "content": "hi"}],
        )
    assert result == "sync result"


def test_resolve_provider_raises_on_unregistered_provider(monkeypatch):
    """If config points at a provider that isn't in the registry,
    surface a clear RuntimeError — better than a confusing AttributeError
    inside the helper.
    """
    fake_registry = MagicMock()
    fake_registry.providers = {}  # empty
    fake_cfg = MagicMock()
    fake_cfg.model.provider = "nonexistent"

    with patch("opencomputer.agent.config.default_config", return_value=fake_cfg), \
         patch("opencomputer.plugins.registry.registry", fake_registry), \
         pytest.raises(RuntimeError, match="not registered"):
        aux_llm._resolve_provider()
