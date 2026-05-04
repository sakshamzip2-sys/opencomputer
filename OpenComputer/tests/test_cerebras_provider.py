"""Tests for the Cerebras provider extension."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_provider_module():
    """Load extensions/cerebras-provider/provider.py without triggering plugin loader."""
    spec_path = (
        Path(__file__).parent.parent
        / "extensions"
        / "cerebras-provider"
        / "provider.py"
    )
    spec = importlib.util.spec_from_file_location(
        "cerebras_provider_test_module", spec_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cerebras_provider_test_module"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cerebras_provider_module_exists():
    mod = _load_provider_module()
    assert hasattr(mod, "CerebrasProvider")


def test_cerebras_provider_default_base_url():
    mod = _load_provider_module()
    assert mod.CEREBRAS_BASE_URL == "https://api.cerebras.ai/v1"


def test_cerebras_provider_reads_api_key_from_env(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-key-123")
    p = mod.CerebrasProvider()
    assert p._api_key() == "test-key-123"


def test_cerebras_provider_raises_without_api_key(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    p = mod.CerebrasProvider()
    with pytest.raises(RuntimeError, match="CEREBRAS_API_KEY"):
        p._api_key()


def test_cerebras_provider_default_models():
    mod = _load_provider_module()
    assert "llama-3.3-70b" in mod.DEFAULT_MODELS
    assert "qwen-3-32b" in mod.DEFAULT_MODELS


@pytest.mark.asyncio
async def test_cerebras_complete_calls_correct_endpoint(monkeypatch):
    """Verify the provider hits api.cerebras.ai/v1/chat/completions."""
    mod = _load_provider_module()
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-key")

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
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
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

    p = mod.CerebrasProvider()
    resp = await p.complete(
        model="llama-3.3-70b",
        messages=[Message(role="user", content="hello")],
        max_tokens=10,
    )
    assert "api.cerebras.ai/v1/chat/completions" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == "llama-3.3-70b"
    assert resp.message.content == "hi"
