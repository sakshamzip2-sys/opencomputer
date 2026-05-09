"""Hermes parity G4: GET + DELETE /v1/responses/{id}."""
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
async def test_get_returns_stored_response():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})

    async def handler(text, sid):
        return "Hello"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer tok"},
            json={"input": "hi"},
        )
        assert r.status == 200
        rid = (await r.json())["id"]

        r2 = await client.get(
            f"/v1/responses/{rid}",
            headers={"Authorization": "Bearer tok"},
        )
        assert r2.status == 200, await r2.text()
        body = await r2.json()
        assert body["id"] == rid


@pytest.mark.asyncio
async def test_get_unknown_returns_404():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get(
            "/v1/responses/nonexistent_id",
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status == 404


@pytest.mark.asyncio
async def test_delete_removes_response():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})

    async def handler(text, sid):
        return "Hello"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer tok"},
            json={"input": "hi"},
        )
        rid = (await r.json())["id"]

        rd = await client.delete(
            f"/v1/responses/{rid}",
            headers={"Authorization": "Bearer tok"},
        )
        assert rd.status == 200
        body = await rd.json()
        assert body.get("deleted") is True

        # Now GET should 404
        r2 = await client.get(
            f"/v1/responses/{rid}",
            headers={"Authorization": "Bearer tok"},
        )
        assert r2.status == 404


@pytest.mark.asyncio
async def test_get_requires_auth():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    a.set_handler(lambda t, s: t)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/v1/responses/anything")
        assert r.status == 401
