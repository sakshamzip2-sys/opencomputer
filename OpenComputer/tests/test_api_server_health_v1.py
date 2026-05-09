"""Hermes parity G5: /v1/health alias mirrors /health."""
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
async def test_v1_health_returns_ok():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/v1/health")
        assert r.status == 200
        body = await r.json()
        assert body.get("status") == "ok"


@pytest.mark.asyncio
async def test_legacy_health_returns_ok():
    """Hermes spec base /health route returns {status: ok} without auth."""
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/health")
        assert r.status == 200
        body = await r.json()
        assert body.get("status") == "ok"


@pytest.mark.asyncio
async def test_health_no_auth_required():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        # No Authorization header
        r = await client.get("/v1/health")
        assert r.status == 200
