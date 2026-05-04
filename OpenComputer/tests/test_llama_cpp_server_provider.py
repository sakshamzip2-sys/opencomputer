"""Tests for the llama-cpp-server provider extension."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_provider_module():
    """Load extensions/llama-cpp-server-provider/provider.py without triggering plugin loader."""
    spec_path = (
        Path(__file__).parent.parent
        / "extensions"
        / "llama-cpp-server-provider"
        / "provider.py"
    )
    spec = importlib.util.spec_from_file_location(
        "llama_cpp_server_provider_test_module", spec_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["llama_cpp_server_provider_test_module"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_module_exists():
    mod = _load_provider_module()
    assert hasattr(mod, "LlamaCppServerProvider")


def test_default_base_url():
    mod = _load_provider_module()
    assert mod.LLAMA_CPP_SERVER_BASE_URL == "http://localhost:8080/v1"


def test_reads_api_key_from_env(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.setenv("LLAMA_CPP_SERVER_API_KEY", "secret-token")
    p = mod.LlamaCppServerProvider()
    assert p._api_key() == "secret-token"


def test_returns_empty_when_api_key_missing(monkeypatch):
    """Local default — no auth required."""
    mod = _load_provider_module()
    monkeypatch.delenv("LLAMA_CPP_SERVER_API_KEY", raising=False)
    p = mod.LlamaCppServerProvider()
    assert p._api_key() == ""


def test_default_models():
    mod = _load_provider_module()
    assert "local-model" in mod.DEFAULT_MODELS


def test_base_url_env_override(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.setenv("LLAMA_CPP_SERVER_BASE_URL", "http://10.0.0.5:9999/v1")
    p = mod.LlamaCppServerProvider()
    assert p.base_url == "http://10.0.0.5:9999/v1"


def test_headers_omits_authorization_when_no_key(monkeypatch):
    """Empty bearer would be rejected by some servers — must omit entirely."""
    mod = _load_provider_module()
    monkeypatch.delenv("LLAMA_CPP_SERVER_API_KEY", raising=False)
    p = mod.LlamaCppServerProvider()
    h = p._headers()
    assert "Authorization" not in h
    assert h["Content-Type"] == "application/json"


def test_headers_includes_bearer_when_key_set(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.setenv("LLAMA_CPP_SERVER_API_KEY", "abc123")
    p = mod.LlamaCppServerProvider()
    h = p._headers()
    assert h["Authorization"] == "Bearer abc123"


@pytest.mark.asyncio
async def test_complete_calls_correct_endpoint(monkeypatch):
    """Verify the provider hits localhost:8080/v1/chat/completions."""
    mod = _load_provider_module()
    monkeypatch.delenv("LLAMA_CPP_SERVER_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CPP_SERVER_BASE_URL", raising=False)

    captured: dict = {}

    class _MockResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

        def raise_for_status(self):
            pass

    class _MockClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            captured["url"] = url
            captured["headers"] = kw.get("headers", {})
            captured["json"] = kw.get("json", {})
            return _MockResponse()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _MockClient)

    from plugin_sdk.core import Message

    p = mod.LlamaCppServerProvider()
    resp = await p.complete(
        model="local-model",
        messages=[Message(role="user", content="hello")],
        max_tokens=10,
    )
    assert "localhost:8080/v1/chat/completions" in captured["url"]
    assert "Authorization" not in captured["headers"]
    assert captured["json"]["model"] == "local-model"
    assert resp.message.content == "hi"
