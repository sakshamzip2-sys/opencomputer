"""PR-A: regression + integration tests for AnthropicProvider credential pool."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path


def _load_anthropic_provider():
    """Load AnthropicProvider fresh from disk, bypassing module cache."""
    repo_root = Path(__file__).resolve().parent.parent
    provider_path = repo_root / "extensions" / "anthropic-provider" / "provider.py"
    module_name = f"_anthropic_provider_pool_test_{id(provider_path)}"
    spec = importlib.util.spec_from_file_location(module_name, provider_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_single_key_env_does_not_construct_pool(monkeypatch):
    """REGRESSION: single key → no pool, behavior identical to today."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-single")
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _load_anthropic_provider()
    provider = mod.AnthropicProvider()
    assert provider._credential_pool is None
    assert provider._api_key == "sk-single"


def test_comma_separated_env_constructs_pool(monkeypatch):
    """Comma-separated key string → pool with correct size."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a,sk-b,sk-c")
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _load_anthropic_provider()
    provider = mod.AnthropicProvider()
    assert provider._credential_pool is not None
    assert provider._credential_pool.size == 3


def test_single_key_no_comma_does_not_construct_pool(monkeypatch):
    """A key without any comma must never construct a pool."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-just-one")
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    mod = _load_anthropic_provider()
    provider = mod.AnthropicProvider()
    assert provider._credential_pool is None
