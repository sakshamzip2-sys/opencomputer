"""Tests for opencomputer.evals.providers — the BaseProvider -> sync .complete shim."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.evals.providers import ProviderShim, get_grader_provider


def test_provider_shim_bridges_async_to_sync():
    """ProviderShim.complete should run the async provider and return an obj with .text."""
    provider = MagicMock()
    response_msg = MagicMock()
    response_msg.content = "the model said this"
    provider_response = MagicMock()
    provider_response.message = response_msg
    provider.complete = AsyncMock(return_value=provider_response)

    shim = ProviderShim(provider, model="claude-sonnet-4-6")
    result = shim.complete("test prompt")

    assert result.text == "the model said this"
    provider.complete.assert_awaited_once()


def test_provider_shim_passes_site_eval_grader():
    """ProviderShim must pass site='eval_grader' through to the provider so
    'oc insights llm' attributes eval traffic separately from agent_loop."""
    received_kwargs = {}

    async def fake_complete(**kwargs):
        received_kwargs.update(kwargs)
        msg = MagicMock()
        msg.content = "ok"
        resp = MagicMock()
        resp.message = msg
        return resp

    fake_provider = MagicMock()
    fake_provider.complete = fake_complete

    shim = ProviderShim(fake_provider, model="claude-sonnet-4-6")
    result = shim.complete("test prompt")

    assert result.text == "ok"
    assert received_kwargs.get("site") == "eval_grader"


def test_get_grader_provider_auto_picks_opus_when_chat_is_sonnet(monkeypatch):
    """If config says Sonnet, grader auto-picks Opus."""
    fake_config = MagicMock()
    fake_config.model.model = "claude-sonnet-4-6"
    fake_config.model.provider = "anthropic"

    fake_provider = MagicMock()
    fake_registry = MagicMock()
    fake_registry.providers = {"anthropic": fake_provider}

    monkeypatch.setattr("opencomputer.agent.config_store.load_config", lambda: fake_config)
    monkeypatch.setattr("opencomputer.plugins.registry.registry", fake_registry)

    shim = get_grader_provider()
    assert shim._model == "claude-opus-4-7"
    assert shim._provider is fake_provider


def test_get_grader_provider_auto_picks_sonnet_when_chat_is_opus(monkeypatch):
    fake_config = MagicMock()
    fake_config.model.model = "claude-opus-4-7"
    fake_config.model.provider = "anthropic"

    fake_provider = MagicMock()
    fake_registry = MagicMock()
    fake_registry.providers = {"anthropic": fake_provider}

    monkeypatch.setattr("opencomputer.agent.config_store.load_config", lambda: fake_config)
    monkeypatch.setattr("opencomputer.plugins.registry.registry", fake_registry)

    shim = get_grader_provider()
    assert shim._model == "claude-sonnet-4-6"


def test_get_grader_provider_raises_for_non_anthropic_without_override(monkeypatch):
    fake_config = MagicMock()
    fake_config.model.model = "deepseek-chat"
    fake_config.model.provider = "deepseek"

    monkeypatch.setattr("opencomputer.agent.config_store.load_config", lambda: fake_config)

    with pytest.raises(RuntimeError, match="auto-pick"):
        get_grader_provider()


def test_get_grader_provider_honors_explicit_override(monkeypatch):
    fake_config = MagicMock()
    fake_config.model.model = "deepseek-chat"
    fake_config.model.provider = "deepseek"

    fake_provider = MagicMock()
    fake_registry = MagicMock()
    fake_registry.providers = {"deepseek": fake_provider}

    monkeypatch.setattr("opencomputer.agent.config_store.load_config", lambda: fake_config)
    monkeypatch.setattr("opencomputer.plugins.registry.registry", fake_registry)

    shim = get_grader_provider(model_override="deepseek-chat-v2")
    assert shim._model == "deepseek-chat-v2"


def test_get_grader_provider_raises_when_provider_not_registered(monkeypatch):
    fake_config = MagicMock()
    fake_config.model.model = "claude-sonnet-4-6"
    fake_config.model.provider = "anthropic"

    fake_registry = MagicMock()
    fake_registry.providers = {}

    monkeypatch.setattr("opencomputer.agent.config_store.load_config", lambda: fake_config)
    monkeypatch.setattr("opencomputer.plugins.registry.registry", fake_registry)

    with pytest.raises(RuntimeError, match="not registered"):
        get_grader_provider()
