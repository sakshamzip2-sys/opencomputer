"""Anthropic provider's per-model native-thinking decision.

Modern models (Sonnet 4+, Opus 4+, Haiku 4.5+) declare native thinking
support → loop skips the prompt-based fallback. Legacy models declare
False → fallback activates so users still see a reasoning panel.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _import_provider():
    mod_name = "_anth_provider_native_thinking"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _import_provider()
    return mod.AnthropicProvider()


@pytest.mark.parametrize("model", [
    "claude-sonnet-4-7",
    "claude-opus-4-7",
])
def test_modern_models_declare_native_thinking(provider, model):
    """Sonnet/Opus 4+ have native extended thinking — the loop should
    skip the prompt-based fallback for these."""
    assert provider.supports_native_thinking_for(model) is True


@pytest.mark.parametrize("model", [
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
])
def test_legacy_models_decline_native_thinking_so_fallback_activates(provider, model):
    """Legacy Claude models (3.5 series, original 3 series) don't have
    native extended thinking; the method must return False so the loop
    wires the prompt-based <think>-tag fallback for those users."""
    assert provider.supports_native_thinking_for(model) is False


def test_method_consistency_with_supports_adaptive_thinking_helper(provider):
    """The method delegates to the canonical
    ``opencomputer.agent.model_capabilities.supports_adaptive_thinking``
    so this provider's decision stays in sync with the rest of the
    Anthropic-specific feature gating (request-time thinking enable,
    budget-token allocation, etc.)."""
    from opencomputer.agent.model_capabilities import supports_adaptive_thinking

    sample_models = [
        "claude-sonnet-4-7",
        "claude-opus-4-7",
        "claude-3-5-sonnet-20241022",
        "claude-3-opus-20240229",
        "unknown-model-xyz",
    ]
    for m in sample_models:
        assert (
            provider.supports_native_thinking_for(m)
            is supports_adaptive_thinking(m)
        ), f"divergence at model={m}"


def test_static_capability_field_is_true_as_fallback(provider):
    """The static capability field is True as the BaseProvider default
    impl's fallback. The instance method overrides it for per-model
    decisions; this test just verifies the static side stays True so
    third-party consumers that read the property only get the
    optimistic answer (modern is the common case)."""
    assert provider.capabilities.supports_native_thinking is True
