"""Tests for OpenRouter provider — thin subclass of OpenAIProvider.

Both ``extensions/openai-provider/`` and ``extensions/openrouter-provider/``
ship a top-level ``provider.py``. We use ``importlib.util.spec_from_file_location``
with explicit unique module names — mirroring what OC's plugin loader does in
production (see ``opencomputer/plugins/loader.py`` and CLAUDE.md §7.1).

Ordering matters when loading OpenRouter:
  1. Load OpenAI parent into ``sys.modules['provider']``
  2. Load OpenRouter under a UNIQUE name so its
     ``from provider import OpenAIProvider`` resolves to step 1
  3. After load, the OpenRouter module is fully constructed; tests use it directly
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_OPENAI_PROVIDER_PY = _REPO / "extensions" / "openai-provider" / "provider.py"
_OPENROUTER_PROVIDER_PY = _REPO / "extensions" / "openrouter-provider" / "provider.py"
_OPENROUTER_PLUGIN_PY = _REPO / "extensions" / "openrouter-provider" / "plugin.py"


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _load_openrouter_class():
    """Load OpenRouterProvider with the OpenAI parent in ``sys.modules['provider']``
    so OpenRouter's ``from provider import OpenAIProvider`` resolves correctly."""
    sys.modules.pop("provider", None)
    _load_module("provider", _OPENAI_PROVIDER_PY)
    sys.modules.pop("openrouter_provider_test", None)
    mod = _load_module("openrouter_provider_test", _OPENROUTER_PROVIDER_PY)
    return mod.OpenRouterProvider


@pytest.fixture
def openrouter_provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    Cls = _load_openrouter_class()
    return Cls()


def test_default_base_url_is_openrouter(openrouter_provider):
    assert openrouter_provider._base == "https://openrouter.ai/api/v1"


def test_api_key_from_env_lands_on_provider(openrouter_provider):
    assert openrouter_provider._api_key == "sk-or-test-key"


def test_api_key_env_class_attr_is_openrouter():
    Cls = _load_openrouter_class()
    assert Cls._api_key_env == "OPENROUTER_API_KEY"


def test_default_model_is_openrouter_shaped():
    Cls = _load_openrouter_class()
    assert "/" in Cls.default_model


def test_base_url_overridable_via_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://custom.example/v1")
    Cls = _load_openrouter_class()
    p = Cls()
    assert p._base == "https://custom.example/v1"


def test_missing_api_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    Cls = _load_openrouter_class()
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        Cls()


def test_pool_mode_with_comma_separated_keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-1,sk-2,sk-3")
    Cls = _load_openrouter_class()
    p = Cls()
    assert p._credential_pool is not None


def test_register_attaches_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-for-register")
    # Load OpenAI parent + OpenRouter (under unique names).
    _load_openrouter_class()
    # plugin.py does `from provider import OpenRouterProvider`. Swap bare
    # 'provider' to the OpenRouter module so that import resolves.
    openrouter_mod = sys.modules["openrouter_provider_test"]
    sys.modules["provider"] = openrouter_mod
    sys.modules.pop("openrouter_plugin_test", None)
    plugin_mod = _load_module("openrouter_plugin_test", _OPENROUTER_PLUGIN_PY)

    registered: dict = {}

    class _StubAPI:
        def register_provider(self, name, cls):
            registered[name] = cls

    plugin_mod.register(_StubAPI())
    assert "openrouter" in registered
    assert registered["openrouter"] is openrouter_mod.OpenRouterProvider
