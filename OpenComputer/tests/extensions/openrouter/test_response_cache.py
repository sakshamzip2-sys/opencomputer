"""Tests for OpenRouter response caching helpers (Wave 5 T5).

The full provider integration is plumbed via the parent OpenAIProvider's
HTTP client, so the canonical test surface is the helper module — the
end-to-end request flow is tested separately by extension-level smoke
tests that mock the wire.
"""

from __future__ import annotations

import importlib.util as _iu
from pathlib import Path

# Direct file-spec load so we don't depend on the dash-named extension
# being importable as a namespace package.
_PROVIDER_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "extensions"
    / "openrouter-provider"
    / "provider.py"
)
_spec = _iu.spec_from_file_location("_or_provider_for_test", str(_PROVIDER_PATH))
_mod = _iu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
build_or_headers = _mod.build_or_headers
parse_cache_status = _mod.parse_cache_status


def test_default_cache_on_with_default_ttl():
    headers = build_or_headers({})
    assert headers.get("X-OpenRouter-Cache") == "1"
    assert int(headers.get("X-OpenRouter-Cache-TTL", "0")) == 300


def test_default_cache_on_when_no_config():
    headers = build_or_headers(None)
    assert headers.get("X-OpenRouter-Cache") == "1"


def test_disable_cache():
    headers = build_or_headers({"openrouter": {"response_cache": False}})
    assert "X-OpenRouter-Cache" not in headers
    assert "X-OpenRouter-Cache-TTL" not in headers


def test_custom_ttl():
    headers = build_or_headers({"openrouter": {"response_cache_ttl": 600}})
    assert headers.get("X-OpenRouter-Cache-TTL") == "600"


def test_ttl_clamped_to_min():
    headers = build_or_headers({"openrouter": {"response_cache_ttl": 0}})
    assert int(headers["X-OpenRouter-Cache-TTL"]) == 1


def test_ttl_clamped_to_max():
    headers = build_or_headers({"openrouter": {"response_cache_ttl": 999_999}})
    assert int(headers["X-OpenRouter-Cache-TTL"]) == 86400


def test_ttl_non_int_falls_back_to_default():
    headers = build_or_headers({"openrouter": {"response_cache_ttl": "abc"}})
    assert int(headers["X-OpenRouter-Cache-TTL"]) == 300


def test_parse_cache_status_hit():
    assert parse_cache_status({"X-OpenRouter-Cache-Status": "HIT"}) == "HIT"


def test_parse_cache_status_lowercase():
    assert parse_cache_status({"x-openrouter-cache-status": "HIT"}) == "HIT"


def test_parse_cache_status_miss_default():
    assert parse_cache_status({}) == "MISS"


def test_parse_cache_status_none_input():
    assert parse_cache_status(None) == "MISS"
