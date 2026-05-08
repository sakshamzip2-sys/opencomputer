"""Hermes parity G3: /v1/responses works by default (no API_SERVER_API_TYPE env)."""
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
async def test_responses_endpoint_works_without_env_gate(monkeypatch):
    monkeypatch.delenv("API_SERVER_API_TYPE", raising=False)
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})

    async def handler(text, sid):
        return "Hello!"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer tok"},
            json={"input": "Hi", "model": "opencomputer"},
        )
        assert r.status == 200, await r.text()
        body = await r.json()
        assert "id" in body


@pytest.mark.asyncio
async def test_responses_still_works_with_env_set(monkeypatch):
    monkeypatch.setenv("API_SERVER_API_TYPE", "responses")
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})

    async def handler(text, sid):
        return "Hello!"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer tok"},
            json={"input": "Hi"},
        )
        assert r.status == 200, "back-compat: env-set still works"
