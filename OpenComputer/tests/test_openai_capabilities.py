"""OpenAI provider declares its capabilities + extracts cached_tokens."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _import_provider():
    mod_name = "_openai_provider_caps"
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
    return _import_provider().OpenAIProvider()


def test_openai_capabilities(provider):
    caps = provider.capabilities
    assert caps.requires_reasoning_resend_in_tool_cycle is False
    assert caps.reasoning_block_kind is None
    assert caps.supports_long_ttl is False
    assert caps.min_cache_tokens("gpt-4o") == 1024


def test_openai_extract_cached_tokens(provider):
    usage = SimpleNamespace(
        prompt_tokens=2000,
        completion_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1700),
    )
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 1700
    assert ct.write == 0


def test_openai_extract_cached_tokens_missing_details(provider):
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 0
    assert ct.write == 0


def test_openai_extract_cached_tokens_details_present_no_cached(provider):
    """prompt_tokens_details exists but cached_tokens is None/missing."""
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        prompt_tokens_details=SimpleNamespace(),
    )
    ct = provider.capabilities.extracts_cache_tokens(usage)
    assert ct.read == 0
    assert ct.write == 0
