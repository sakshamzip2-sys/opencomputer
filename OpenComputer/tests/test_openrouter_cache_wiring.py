"""Tests for OpenRouter cache header wiring — Wave 5 T5 closure.

Hermes-port (457c7b76c). Verifies that ``build_or_headers`` is invoked at
``OpenRouterProvider.__init__`` and the resulting headers reach the
underlying ``AsyncOpenAI`` client's ``default_headers``.

Response-side parsing (``parse_cache_status``) is intentionally NOT
wired in this commit — would require ``client.with_raw_response`` plumbing
through the parent OpenAI provider, deferred to a future PR.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _load_or_provider():
    """Load the OpenRouter provider module under a unique name to avoid the
    `provider` collision documented in the source file's preamble."""
    p = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "openrouter-provider"
        / "provider.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_or_provider_for_T5", str(p)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def or_mod():
    return _load_or_provider()


@pytest.fixture(autouse=True)
def _stub_api_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")


def test_default_cache_headers_present_on_construction(or_mod):
    p = or_mod.OpenRouterProvider()
    assert p._or_cache_headers["X-OpenRouter-Cache"] == "1"
    # Default TTL is 300 (5 min, per Hermes spec)
    assert p._or_cache_headers["X-OpenRouter-Cache-TTL"] == "300"


def test_async_openai_client_carries_cache_headers(or_mod):
    p = or_mod.OpenRouterProvider()
    headers = p.client.default_headers
    assert headers.get("X-OpenRouter-Cache") == "1"
    assert headers.get("X-OpenRouter-Cache-TTL") == "300"


def test_explicit_disable_via_config(monkeypatch, or_mod):
    """When openrouter.response_cache=False, no cache headers should be added."""
    # Patch _load_or_cfg directly — simpler than rigging the OC config_store
    monkeypatch.setattr(
        or_mod.OpenRouterProvider,
        "_load_or_cfg",
        staticmethod(lambda: {"openrouter": {"response_cache": False}}),
    )
    p = or_mod.OpenRouterProvider()
    assert p._or_cache_headers == {}
    # Client still constructs but without cache headers
    assert "X-OpenRouter-Cache" not in p.client.default_headers


def test_custom_ttl_clamped_to_max(monkeypatch, or_mod):
    monkeypatch.setattr(
        or_mod.OpenRouterProvider,
        "_load_or_cfg",
        staticmethod(lambda: {
            "openrouter": {"response_cache": True, "response_cache_ttl": 999_999},
        }),
    )
    p = or_mod.OpenRouterProvider()
    # 24h cap = 86400
    assert p._or_cache_headers["X-OpenRouter-Cache-TTL"] == "86400"


def test_custom_ttl_clamped_to_min(monkeypatch, or_mod):
    monkeypatch.setattr(
        or_mod.OpenRouterProvider,
        "_load_or_cfg",
        staticmethod(lambda: {
            "openrouter": {"response_cache": True, "response_cache_ttl": 0},
        }),
    )
    p = or_mod.OpenRouterProvider()
    # min = 1
    assert p._or_cache_headers["X-OpenRouter-Cache-TTL"] == "1"


def test_config_load_failure_falls_back_to_defaults(monkeypatch, or_mod):
    """Construction must never break on a malformed/missing OC config."""

    def boom():
        raise RuntimeError("config file unreadable")

    monkeypatch.setattr(
        or_mod.OpenRouterProvider,
        "_load_or_cfg",
        staticmethod(boom),
    )
    # Should not raise; provider falls back to no-cache (cache_headers = {})
    p = or_mod.OpenRouterProvider()
    assert p._or_cache_headers == {}


def test_parse_cache_status_helper_unchanged(or_mod):
    """parse_cache_status() public helper still returns expected values
    (response-side parsing isn't wired but the helper itself is exported
    for callers building observability layers on top of the provider)."""
    assert or_mod.parse_cache_status({"X-OpenRouter-Cache-Status": "HIT"}) == "HIT"
    assert or_mod.parse_cache_status({}) == "MISS"
    assert or_mod.parse_cache_status({"x-openrouter-cache-status": "MISS"}) == "MISS"
