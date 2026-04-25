"""PR-A: regression + integration tests for OpenAIProvider credential pool."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path


def _load_openai_provider():
    """Load OpenAIProvider fresh from disk, bypassing module cache."""
    repo_root = Path(__file__).resolve().parent.parent
    provider_path = repo_root / "extensions" / "openai-provider" / "provider.py"
    module_name = f"_openai_provider_pool_test_{id(provider_path)}"
    spec = importlib.util.spec_from_file_location(module_name, provider_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_single_key_env_does_not_construct_pool(monkeypatch):
    """REGRESSION: single key → no pool, behavior identical to today."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-single")
    mod = _load_openai_provider()
    provider = mod.OpenAIProvider()
    assert provider._credential_pool is None
    assert provider._api_key == "sk-openai-single"


def test_comma_separated_env_constructs_pool(monkeypatch):
    """Comma-separated key string → pool with correct size."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-a,sk-b,sk-c")
    mod = _load_openai_provider()
    provider = mod.OpenAIProvider()
    assert provider._credential_pool is not None
    assert provider._credential_pool.size == 3


def test_single_key_no_comma_does_not_construct_pool(monkeypatch):
    """A key without any comma must never construct a pool."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-just-one")
    mod = _load_openai_provider()
    provider = mod.OpenAIProvider()
    assert provider._credential_pool is None
