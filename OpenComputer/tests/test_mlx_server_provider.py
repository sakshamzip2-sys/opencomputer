"""Tests for the MLX-server provider extension."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_provider_module():
    """Load extensions/mlx-server-provider/provider.py without triggering plugin loader."""
    spec_path = (
        Path(__file__).parent.parent
        / "extensions"
        / "mlx-server-provider"
        / "provider.py"
    )
    spec = importlib.util.spec_from_file_location(
        "mlx_server_provider_test_module", spec_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mlx_server_provider_test_module"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_module_exists():
    mod = _load_provider_module()
    assert hasattr(mod, "MLXServerProvider")


def test_default_base_url():
    mod = _load_provider_module()
    assert mod.MLX_SERVER_BASE_URL == "http://localhost:8081/v1"


def test_default_port_avoids_llama_cpp_clash():
    """Port 8081 was chosen specifically to coexist with llama.cpp (8080)."""
    mod = _load_provider_module()
    assert ":8081/" in mod.MLX_SERVER_BASE_URL
    assert ":8080/" not in mod.MLX_SERVER_BASE_URL


def test_reads_api_key_from_env(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.setenv("MLX_SERVER_API_KEY", "mlx-tok")
    p = mod.MLXServerProvider()
    assert p._api_key() == "mlx-tok"


def test_returns_empty_when_api_key_missing(monkeypatch):
    """Local default — no auth required."""
    mod = _load_provider_module()
    monkeypatch.delenv("MLX_SERVER_API_KEY", raising=False)
    p = mod.MLXServerProvider()
    assert p._api_key() == ""


def test_default_models_use_mlx_community_namespace():
    mod = _load_provider_module()
    assert any(m.startswith("mlx-community/") for m in mod.DEFAULT_MODELS)


def test_base_url_env_override(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.setenv("MLX_SERVER_BASE_URL", "http://192.168.1.30:8081/v1")
    p = mod.MLXServerProvider()
    assert p.base_url == "http://192.168.1.30:8081/v1"


def test_headers_omits_authorization_when_no_key(monkeypatch):
    mod = _load_provider_module()
    monkeypatch.delenv("MLX_SERVER_API_KEY", raising=False)
    p = mod.MLXServerProvider()
    h = p._headers()
    assert "Authorization" not in h


@pytest.mark.asyncio
async def test_complete_calls_correct_endpoint(monkeypatch):
    """Verify the provider hits localhost:8081/v1/chat/completions."""
    mod = _load_provider_module()
    monkeypatch.delenv("MLX_SERVER_API_KEY", raising=False)
    monkeypatch.delenv("MLX_SERVER_BASE_URL", raising=False)

    captured: dict = {}

    class _MockResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ack"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
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

    p = mod.MLXServerProvider()
    resp = await p.complete(
        model="mlx-community/Llama-3.1-8B-Instruct-4bit",
        messages=[Message(role="user", content="hi")],
        max_tokens=10,
    )
    assert "localhost:8081/v1/chat/completions" in captured["url"]
    assert "Authorization" not in captured["headers"]
    assert captured["json"]["model"] == "mlx-community/Llama-3.1-8B-Instruct-4bit"
    assert resp.message.content == "ack"
