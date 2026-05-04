"""Tests for the LM Studio provider extension."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_provider_module():
    """Load extensions/lmstudio-provider/provider.py without triggering plugin loader."""
    spec_path = (
        Path(__file__).parent.parent
        / "extensions"
        / "lmstudio-provider"
        / "provider.py"
    )
    spec = importlib.util.spec_from_file_location(
        "lmstudio_provider_test_module", spec_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lmstudio_provider_test_module"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_module_exists():
    mod = _load_provider_module()
    assert hasattr(mod, "LMStudioProvider")


def test_default_base_url():
    mod = _load_provider_module()
    assert mod.LMSTUDIO_BASE_URL == "http://localhost:1234/v1"


def test_default_api_key_is_lm_studio_literal():
    mod = _load_provider_module()
    assert mod.LMSTUDIO_DEFAULT_API_KEY == "lm-studio"


def test_reads_api_key_from_env(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.setenv("LMSTUDIO_API_KEY", "custom-key-99")
    p = mod.LMStudioProvider()
    assert p._api_key() == "custom-key-99"


def test_falls_back_to_lm_studio_default(monkeypatch):
    """LM Studio's literal default is the string 'lm-studio'."""
    mod = _load_provider_module()
    monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
    p = mod.LMStudioProvider()
    assert p._api_key() == "lm-studio"


def test_default_models():
    mod = _load_provider_module()
    assert "local-model" in mod.DEFAULT_MODELS


def test_base_url_env_override(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://192.168.1.20:1234/v1")
    p = mod.LMStudioProvider()
    assert p.base_url == "http://192.168.1.20:1234/v1"


def test_headers_omits_authorization_when_key_explicitly_empty(monkeypatch):
    """User can opt out of auth by setting LMSTUDIO_API_KEY=''."""
    mod = _load_provider_module()
    monkeypatch.setenv("LMSTUDIO_API_KEY", "")
    p = mod.LMStudioProvider()
    h = p._headers()
    assert "Authorization" not in h


def test_headers_uses_default_bearer_when_unset(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
    p = mod.LMStudioProvider()
    h = p._headers()
    assert h["Authorization"] == "Bearer lm-studio"


@pytest.mark.asyncio
async def test_complete_calls_correct_endpoint(monkeypatch):
    """Verify the provider hits localhost:1234/v1/chat/completions."""
    mod = _load_provider_module()
    monkeypatch.setenv("LMSTUDIO_API_KEY", "test-key")
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)

    captured: dict = {}

    class _MockResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
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

    p = mod.LMStudioProvider()
    resp = await p.complete(
        model="local-model",
        messages=[Message(role="user", content="hello")],
        max_tokens=10,
    )
    assert "localhost:1234/v1/chat/completions" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == "local-model"
    assert resp.message.content == "ok"
