"""Tests for ModelConfig.api_mode + Azure Foundry Anthropic-style dispatch.

api_mode lets a single provider plugin dispatch through different wire
shapes (OpenAI's chat/completions vs Anthropic's /v1/messages) on a
per-model basis. Use case: Azure AI Foundry deployments that route Claude
under Anthropic's wire format and GPT-4o under OpenAI's, both reachable
through the same provider.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_OPENAI_PROVIDER_PY = _REPO / "extensions" / "openai-provider" / "provider.py"
_ANTHROPIC_PROVIDER_PY = _REPO / "extensions" / "anthropic-provider" / "provider.py"
_AZURE_PROVIDER_PY = _REPO / "extensions" / "azure-foundry-provider" / "provider.py"


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _load_azure():
    sys.modules.pop("provider", None)
    _load_module("provider", _OPENAI_PROVIDER_PY)
    sys.modules.pop("provider_anthropic", None)
    _load_module("provider_anthropic", _ANTHROPIC_PROVIDER_PY)
    sys.modules.pop("azure_test", None)
    return _load_module("azure_test", _AZURE_PROVIDER_PY)


# --------------------------------------------------------------------------- #
# ModelConfig.api_mode
# --------------------------------------------------------------------------- #

def test_model_config_has_api_mode_field_with_default_auto():
    from opencomputer.agent.config import ModelConfig

    cfg = ModelConfig()
    assert cfg.api_mode == "auto"


def test_model_config_accepts_openai_api_mode():
    from opencomputer.agent.config import ModelConfig

    cfg = ModelConfig(api_mode="openai")
    assert cfg.api_mode == "openai"


def test_model_config_accepts_anthropic_api_mode():
    from opencomputer.agent.config import ModelConfig

    cfg = ModelConfig(api_mode="anthropic")
    assert cfg.api_mode == "anthropic"


def test_model_config_rejects_unknown_api_mode():
    from opencomputer.agent.config import ModelConfig

    with pytest.raises(ValueError, match="api_mode"):
        ModelConfig(api_mode="grpc")


def test_model_config_remains_hashable_with_api_mode():
    """frozen+slots dataclasses get auto __hash__; ensure api_mode str preserves it."""
    from opencomputer.agent.config import ModelConfig

    cfg = ModelConfig(api_mode="anthropic")
    hash(cfg)  # Should not raise


def test_model_config_api_mode_round_trips_through_yaml(tmp_path, monkeypatch):
    """A user-provided api_mode in config.yaml survives load/save."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    config_dir = tmp_path / "default"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "model:\n"
        "  provider: azure-foundry\n"
        "  model: claude-3-5-sonnet\n"
        "  api_mode: anthropic\n"
    )
    from opencomputer.agent.config_store import load_config

    cfg = load_config(config_dir / "config.yaml")
    assert cfg.model.api_mode == "anthropic"


# --------------------------------------------------------------------------- #
# Azure Foundry transport switching
# --------------------------------------------------------------------------- #

def test_azure_foundry_default_uses_openai_transport(monkeypatch):
    """api_mode=auto (default) and api_mode=openai both route via OpenAI shape."""
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "key-x")
    monkeypatch.setenv("AZURE_FOUNDRY_BASE_URL", "https://x.openai.azure.com/openai")
    mod = _load_azure()
    p = mod.AzureFoundryProvider()
    # Default transport — OpenAI shape exposes _api_key + _base
    assert hasattr(p, "_api_key")
    assert p._api_key == "key-x"
    assert getattr(p, "_api_mode", "auto") in {"auto", "openai"}


def test_azure_foundry_explicit_openai_mode_uses_openai_transport(monkeypatch):
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "key-x")
    monkeypatch.setenv("AZURE_FOUNDRY_BASE_URL", "https://x.openai.azure.com/openai")
    mod = _load_azure()
    p = mod.AzureFoundryProvider(api_mode="openai")
    assert p._api_key == "key-x"


def test_azure_foundry_anthropic_mode_uses_anthropic_transport(monkeypatch):
    """api_mode=anthropic returns a provider backed by AnthropicProvider transport."""
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "key-x")
    monkeypatch.setenv(
        "AZURE_FOUNDRY_BASE_URL",
        "https://x.openai.azure.com/anthropic",
    )
    mod = _load_azure()
    p = mod.AzureFoundryProvider(api_mode="anthropic")
    # The Anthropic-shaped provider's class signals via _api_mode attribute
    assert getattr(p, "_api_mode", None) == "anthropic"


def test_azure_foundry_rejects_unknown_api_mode(monkeypatch):
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "key-x")
    monkeypatch.setenv("AZURE_FOUNDRY_BASE_URL", "https://x")
    mod = _load_azure()
    with pytest.raises(ValueError, match="api_mode"):
        mod.AzureFoundryProvider(api_mode="grpc")


def test_azure_foundry_resolves_api_mode_from_env(monkeypatch):
    """AZURE_FOUNDRY_API_MODE env var is honored when constructor arg is absent."""
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "key-x")
    monkeypatch.setenv("AZURE_FOUNDRY_BASE_URL", "https://x")
    monkeypatch.setenv("AZURE_FOUNDRY_API_MODE", "anthropic")
    mod = _load_azure()
    p = mod.AzureFoundryProvider()
    assert getattr(p, "_api_mode", None) == "anthropic"


# --------------------------------------------------------------------------- #
# Plumbing: _resolve_provider passes api_mode when the provider accepts it
# --------------------------------------------------------------------------- #

def test_resolve_provider_threads_api_mode_when_supported(monkeypatch):
    """If a registered provider accepts api_mode kwarg, _resolve_provider passes it."""
    from opencomputer.cli import _resolve_provider
    from opencomputer.plugins.registry import registry as plugin_registry

    captured = {}

    class _StubProvider:
        def __init__(self, api_mode: str = "auto"):
            captured["api_mode"] = api_mode

    plugin_registry.providers["stub-with-api-mode"] = _StubProvider
    try:
        _resolve_provider("stub-with-api-mode", api_mode="anthropic")
        assert captured["api_mode"] == "anthropic"
    finally:
        plugin_registry.providers.pop("stub-with-api-mode", None)


def test_resolve_provider_does_not_break_providers_without_api_mode():
    """Providers that don't accept api_mode still construct cleanly."""
    from opencomputer.cli import _resolve_provider
    from opencomputer.plugins.registry import registry as plugin_registry

    class _LegacyProvider:
        def __init__(self):
            self.constructed = True

    plugin_registry.providers["stub-legacy"] = _LegacyProvider
    try:
        p = _resolve_provider("stub-legacy", api_mode="anthropic")
        assert p.constructed is True
    finally:
        plugin_registry.providers.pop("stub-legacy", None)
