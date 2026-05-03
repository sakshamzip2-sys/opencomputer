"""OpenAI provider's per-model native-reasoning decision.

Only o-series and gpt-5+ have native reasoning. For gpt-4o, gpt-4,
gpt-3.5 etc. the loop activates the prompt-based <think>-tag fallback
so those users get model-agnostic thinking visibility too.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _import_provider():
    mod_name = "_openai_provider_native_thinking"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "openai-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    mod = _import_provider()
    return mod.OpenAIProvider()


@pytest.mark.parametrize("model,expected", [
    ("o1-preview", True),
    ("o1-mini", True),
    ("o3-mini", True),
    ("o4", True),
    ("gpt-5", True),
    ("gpt-5-turbo", True),
    ("openai/o1", True),         # OpenRouter-style prefix
    ("openai/o3-mini", True),
    ("openai/gpt-5", True),
    ("gpt-4o", False),
    ("gpt-4-turbo", False),
    ("gpt-4", False),
    ("gpt-3.5-turbo", False),
    ("openai/gpt-4o", False),
    ("openai/gpt-4-turbo", False),
    ("", False),                  # empty / unknown
    ("random-model-xyz", False),
])
def test_supports_native_thinking_for_per_model(provider, model, expected):
    assert provider.supports_native_thinking_for(model) is expected


def test_static_capability_field_is_true_as_fallback(provider):
    """Static side defaults True — third-party consumers that read the
    property only get the optimistic answer (o-series is the intended
    default model class for thinking)."""
    assert provider.capabilities.supports_native_thinking is True
