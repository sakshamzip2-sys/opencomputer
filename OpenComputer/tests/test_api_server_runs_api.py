"""T59 — /v1/runs Runs API (POST create / GET status / GET events SSE / POST stop)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer


def _load_adapter_module():
    if "api_server_adapter_test" in sys.modules:
        return sys.modules["api_server_adapter_test"]
    spec_path = (
        Path(__file__).parent.parent / "extensions" / "api-server" / "adapter.py"
    )
    spec = importlib.util.spec_from_file_location("api_server_adapter_test", spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def adapter_mod():
    return _load_adapter_module()


_AUTH = {"Authorization": "Bearer tok"}


def _adapter_with_handler(adapter_mod):
    cfg = {"host": "127.0.0.1", "port": 0, "token": "tok"}
    a = adapter_mod.APIServerAdapter(cfg)

    async def handler(text: str, session_id: str) -> str:
        return f"reply: {text}"

    a.set_handler(handler)
    return a


@pytest.mark.asyncio
async def test_post_run_creates_returns_run_id(adapter_mod):
    a = _adapter_with_handler(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/v1/runs", json={"input": "hi"}, headers=_AUTH)
        assert r.status == 200
        body = await r.json()
        assert body["run_id"].startswith("run-")
        assert body["status"] == "pending"


@pytest.mark.asyncio
async def test_get_run_status_after_completion(adapter_mod):
    a = _adapter_with_handler(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post("/v1/runs", json={"input": "hello"}, headers=_AUTH)
        run_id = (await r1.json())["run_id"]
        # Wait for completion (handler is fast).
        for _ in range(40):
            r2 = await client.get(f"/v1/runs/{run_id}", headers=_AUTH)
            body = await r2.json()
            if body["status"] in ("done", "error"):
                break
            await asyncio.sleep(0.01)
        assert body["status"] == "done"
        assert body["result"] == "reply: hello"


@pytest.mark.asyncio
async def test_get_run_events_replays_buffered(adapter_mod):
    a = _adapter_with_handler(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post("/v1/runs", json={"input": "world"}, headers=_AUTH)
        run_id = (await r1.json())["run_id"]
        # Wait for completion before fetching events (replay path).
        for _ in range(40):
            r2 = await client.get(f"/v1/runs/{run_id}", headers=_AUTH)
            if (await r2.json())["status"] == "done":
                break
            await asyncio.sleep(0.01)
        r3 = await client.get(f"/v1/runs/{run_id}/events", headers=_AUTH)
        text = await r3.text()
        assert "run.created" in text
        assert "run.completed" in text
        assert "[DONE]" in text


@pytest.mark.asyncio
async def test_get_run_unknown_id_404(adapter_mod):
    a = _adapter_with_handler(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/v1/runs/run-nope", headers=_AUTH)
        assert r.status == 404


@pytest.mark.asyncio
async def test_post_run_no_input_400(adapter_mod):
    a = _adapter_with_handler(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/v1/runs", json={}, headers=_AUTH)
        assert r.status == 400


@pytest.mark.asyncio
async def test_runs_api_capability_advertised(adapter_mod):
    a = _adapter_with_handler(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/v1/capabilities")
        body = await r.json()
        assert body["features"]["runs_api"] is True
