"""Tests for the /capabilities slash command."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from plugin_sdk.runtime_context import RuntimeContext


def _runtime() -> RuntimeContext:
    return RuntimeContext(custom={})


def _provider_with_overrides(name: str, *, overrides: tuple[str, ...]) -> type:
    """Build a fake provider CLASS that overrides the given BaseProvider methods.

    Capability detection introspects ``cls.__dict__`` for the method
    names, so we need a real class — not just a MagicMock that pretends
    to have attributes.
    """
    from plugin_sdk.provider_contract import BaseProvider

    body = {"name": name, "default_model": "stub-1"}

    async def _fake_complete(**_kw):
        raise NotImplementedError

    async def _fake_stream_complete(**_kw):
        raise NotImplementedError
        yield

    body["complete"] = _fake_complete
    body["stream_complete"] = _fake_stream_complete

    if "complete_vision" in overrides:
        async def _fake_vision(**_kw):
            return "ok"
        body["complete_vision"] = _fake_vision
    if "submit_batch" in overrides:
        async def _fake_batch(*_a, **_kw):
            return "batch_id"
        body["submit_batch"] = _fake_batch

    return type(name.title() + "Provider", (BaseProvider,), body)


@pytest.mark.asyncio
async def test_capabilities_lists_current_and_alternatives(monkeypatch):
    from opencomputer.agent.slash_commands_impl.capabilities_cmd import (
        CapabilitiesCommand,
    )

    AnthropicCls = _provider_with_overrides(
        "anthropic", overrides=("complete_vision", "submit_batch"),
    )
    OpenAICls = _provider_with_overrides("openai", overrides=("complete_vision",))
    OllamaCls = _provider_with_overrides("ollama", overrides=())

    fake_registry = MagicMock()
    fake_registry.providers = {
        "anthropic": AnthropicCls,
        "openai": OpenAICls,
        "ollama": OllamaCls,
    }

    fake_cfg = MagicMock()
    fake_cfg.model.provider = "openai"
    fake_cfg.model.name = "gpt-5.4"

    monkeypatch.setattr("opencomputer.plugins.registry.registry", fake_registry)
    monkeypatch.setattr("opencomputer.agent.config.default_config", lambda: fake_cfg)

    cmd = CapabilitiesCommand()
    result = await cmd.execute("", _runtime())

    output = result.output
    # Names current provider with model
    assert "openai" in output
    assert "gpt-5.4" in output
    # Lists current capability
    assert "vision" in output
    # Lists OTHER providers' capabilities
    assert "anthropic" in output
    # Mentions ollama (collapsed line for empty-cap providers)
    assert "ollama" in output


@pytest.mark.asyncio
async def test_capabilities_shows_missing_with_switch_hint(monkeypatch):
    """When current provider lacks capabilities OTHER providers have,
    the output highlights what's missing AND tells the user how to switch.
    """
    from opencomputer.agent.slash_commands_impl.capabilities_cmd import (
        CapabilitiesCommand,
    )

    # Current = openai (vision only); anthropic has both vision + batch
    AnthropicCls = _provider_with_overrides(
        "anthropic", overrides=("complete_vision", "submit_batch"),
    )
    OpenAICls = _provider_with_overrides("openai", overrides=("complete_vision",))

    fake_registry = MagicMock()
    fake_registry.providers = {"anthropic": AnthropicCls, "openai": OpenAICls}

    fake_cfg = MagicMock()
    fake_cfg.model.provider = "openai"
    fake_cfg.model.name = "gpt-5.4"

    monkeypatch.setattr("opencomputer.plugins.registry.registry", fake_registry)
    monkeypatch.setattr("opencomputer.agent.config.default_config", lambda: fake_cfg)

    cmd = CapabilitiesCommand()
    result = await cmd.execute("", _runtime())

    out = result.output
    # batch is missing on current
    assert "batch" in out and "Missing" in out
    # Mentions how to switch
    assert "/provider" in out or "oc model" in out
    # Names anthropic as having batch
    assert "anthropic" in out


@pytest.mark.asyncio
async def test_capabilities_handles_unconfigured_provider(monkeypatch):
    """If the configured provider isn't in the registry, the command
    surfaces 'NOT REGISTERED' rather than crashing.
    """
    from opencomputer.agent.slash_commands_impl.capabilities_cmd import (
        CapabilitiesCommand,
    )

    fake_registry = MagicMock()
    fake_registry.providers = {}

    fake_cfg = MagicMock()
    fake_cfg.model.provider = "mystery"
    fake_cfg.model.name = "x"

    monkeypatch.setattr("opencomputer.plugins.registry.registry", fake_registry)
    monkeypatch.setattr("opencomputer.agent.config.default_config", lambda: fake_cfg)

    cmd = CapabilitiesCommand()
    result = await cmd.execute("", _runtime())
    out = result.output
    assert "mystery" in out
    assert "NOT REGISTERED" in out
    assert result.handled


@pytest.mark.asyncio
async def test_subclass_inherits_capability_via_mro(monkeypatch):
    """OpenRouter extends OpenAI; if OpenAI overrides complete_vision,
    OpenRouter inherits the capability without overriding it itself.
    """
    from opencomputer.agent.slash_commands_impl.capabilities_cmd import (
        _capabilities_for,
        _provider_supports,
    )

    OpenAICls = _provider_with_overrides("openai", overrides=("complete_vision",))

    class OpenRouterCls(OpenAICls):
        name = "openrouter"

    # OpenRouter inherits via MRO — _provider_supports finds it
    assert _provider_supports(OpenRouterCls, "vision")
    assert "vision" in _capabilities_for(OpenRouterCls)


def test_metadata():
    from opencomputer.agent.slash_commands_impl.capabilities_cmd import (
        CapabilitiesCommand,
    )

    cmd = CapabilitiesCommand()
    assert cmd.name == "capabilities"
    assert "provider" in cmd.description.lower() or "feature" in cmd.description.lower()
