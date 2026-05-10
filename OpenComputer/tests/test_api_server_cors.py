"""Hermes parity G1: CORS preflight + headers per API_SERVER_CORS_ORIGINS."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer


def _load_adapter():
    if "api_server_adapter_test" in sys.modules:
        return sys.modules["api_server_adapter_test"]
    spec_path = Path(__file__).parent.parent / "extensions" / "api-server" / "adapter.py"
    spec = importlib.util.spec_from_file_location("api_server_adapter_test", spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_preflight_returns_200_with_allowed_headers(monkeypatch):
    monkeypatch.setenv("API_SERVER_CORS_ORIGINS", "http://localhost:3000")
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.options(
            "/v1/chat/completions",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Content-Type,Idempotency-Key",
            },
        )
        assert r.status == 200, await r.text()
        assert r.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
        assert r.headers["Access-Control-Max-Age"] == "600"
        assert "Authorization" in r.headers["Access-Control-Allow-Headers"]
        assert "Idempotency-Key" in r.headers["Access-Control-Allow-Headers"]


@pytest.mark.asyncio
async def test_post_includes_cors_origin_when_allowed(monkeypatch):
    monkeypatch.setenv("API_SERVER_CORS_ORIGINS", "http://localhost:3000")
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})

    async def handler(text, sid):
        return "ok"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok", "Origin": "http://localhost:3000"},
            json={"text": "hi"},
        )
        assert r.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"


@pytest.mark.asyncio
async def test_no_cors_origin_header_when_origin_disallowed(monkeypatch):
    monkeypatch.setenv("API_SERVER_CORS_ORIGINS", "http://localhost:3000")
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.options(
            "/v1/chat/completions",
            headers={
                "Origin": "http://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "Access-Control-Allow-Origin" not in r.headers


@pytest.mark.asyncio
async def test_no_cors_when_env_unset(monkeypatch):
    monkeypatch.delenv("API_SERVER_CORS_ORIGINS", raising=False)
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.options(
            "/v1/chat/completions",
            headers={"Origin": "http://localhost:3000"},
        )
        assert "Access-Control-Allow-Origin" not in r.headers
