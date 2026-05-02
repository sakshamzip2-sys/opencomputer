"""Tests for the DeepSeek provider plugin (P1.a).

Loads modules via ``importlib.util.spec_from_file_location`` with unique
names so this test doesn't collide with the openrouter / openai provider
modules that also ship a top-level ``provider.py``. Same pattern as
``tests/test_openrouter_provider.py``.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_OPENAI_PROVIDER_PY = _REPO / "extensions" / "openai-provider" / "provider.py"
_DEEPSEEK_PROVIDER_PY = _REPO / "extensions" / "deepseek-provider" / "provider.py"
_DEEPSEEK_PLUGIN_JSON = _REPO / "extensions" / "deepseek-provider" / "plugin.json"


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _load_deepseek_class():
    """Load DeepSeekProvider with the OpenAI parent in ``sys.modules['provider']``
    so DeepSeek's ``from provider import OpenAIProvider`` resolves correctly."""
    sys.modules.pop("provider", None)
    _load_module("provider", _OPENAI_PROVIDER_PY)
    sys.modules.pop("deepseek_provider_test", None)
    mod = _load_module("deepseek_provider_test", _DEEPSEEK_PROVIDER_PY)
    return mod.DeepSeekProvider


@pytest.fixture
def deepseek_provider(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test-key")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    Cls = _load_deepseek_class()
    return Cls()


def test_default_base_url_is_deepseek(deepseek_provider):
    assert deepseek_provider._base.startswith("https://api.deepseek.com")


def test_api_key_from_env_lands_on_provider(deepseek_provider):
    assert deepseek_provider._api_key == "sk-deepseek-test-key"


def test_api_key_env_class_attr_is_deepseek():
    Cls = _load_deepseek_class()
    assert Cls._api_key_env == "DEEPSEEK_API_KEY"


def test_default_model_is_deepseek_chat():
    Cls = _load_deepseek_class()
    assert Cls.default_model == "deepseek-chat"


def test_name_class_attr_is_deepseek():
    Cls = _load_deepseek_class()
    assert Cls.name == "deepseek"


def test_base_url_override_via_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://custom.example/v1")
    Cls = _load_deepseek_class()
    p = Cls()
    assert p._base == "https://custom.example/v1"


def test_missing_api_key_raises_with_helpful_message(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    Cls = _load_deepseek_class()
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        Cls()


def test_does_not_fall_back_to_openai_api_key(monkeypatch):
    """Setting OPENAI_API_KEY must NOT satisfy the deepseek check."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-other-provider")
    Cls = _load_deepseek_class()
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        Cls()


def test_plugin_manifest_setup_provider_declares_correct_env_var():
    manifest = json.loads(_DEEPSEEK_PLUGIN_JSON.read_text())
    setup = manifest["setup"]["providers"][0]
    assert setup["id"] == "deepseek"
    assert setup["env_vars"] == ["DEEPSEEK_API_KEY"]
    assert "deepseek" in setup["label"].lower()
    assert setup["default_model"] == "deepseek-chat"


def test_plugin_appears_in_wizard_provider_discovery():
    """Wizard's _discover_providers must surface the new manifest."""
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        _discover_providers,
    )
    providers = _discover_providers()
    ids = [p["name"] for p in providers]  # 'name' key is the provider id
    assert "deepseek" in ids
