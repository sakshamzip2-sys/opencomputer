"""OpenRouter provider's cache-token extractor reads either Anthropic-style
or OpenAI-style fields, depending on which upstream answered."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _import_provider():
    mod_name = "_or_provider_caps"
    if mod_name in sys.modules and hasattr(sys.modules[mod_name], "OpenRouterProvider"):
        return sys.modules[mod_name]
    repo = Path(__file__).resolve().parent.parent
    # The openrouter provider does ``from provider import OpenAIProvider`` at
    # module load. When other tests have stuffed something else into
    # sys.modules['provider'], that import resolves to the wrong module and
    # OpenRouterProvider's class body never executes. Pre-stage the openai
    # provider under the literal name 'provider' so the openrouter module's
    # import succeeds regardless of test ordering.
    openai_path = repo / "extensions" / "openai-provider" / "provider.py"
    spec_oa = importlib.util.spec_from_file_location("provider", openai_path)
    assert spec_oa is not None and spec_oa.loader is not None
    mod_oa = importlib.util.module_from_spec(spec_oa)
    sys.modules["provider"] = mod_oa
    spec_oa.loader.exec_module(mod_oa)
    plugin_path = repo / "extensions" / "openrouter-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    return _import_provider().OpenRouterProvider()


def test_openrouter_capabilities_safe_defaults(provider):
    caps = provider.capabilities
    assert caps.requires_reasoning_resend_in_tool_cycle is False
    assert caps.reasoning_block_kind is None
    assert caps.supports_long_ttl is False


def test_openrouter_extracts_anthropic_shape(provider):
    """When OpenRouter routes to Anthropic, usage carries Anthropic field names."""
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=1500,
    )
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 1500
    assert ct.write == 200


def test_openrouter_extracts_openai_shape(provider):
    """When OpenRouter routes to an OpenAI-compatible upstream."""
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        prompt_tokens_details=SimpleNamespace(cached_tokens=900),
    )
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 900
    assert ct.write == 0


def test_openrouter_no_cache_fields(provider):
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 0
    assert ct.write == 0


def test_openrouter_anthropic_shape_only_read(provider):
    """Read-only Anthropic shape (write=0) still gets parsed correctly."""
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        cache_read_input_tokens=500,
    )
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 500
    assert ct.write == 0
