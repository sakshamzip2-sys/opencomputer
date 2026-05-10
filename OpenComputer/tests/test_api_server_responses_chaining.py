"""/v1/responses chaining: previous_response_id + named conversation."""

from __future__ import annotations

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


def _make_adapter(adapter_mod):
    cfg = {"host": "127.0.0.1", "port": 0, "token": "tok"}
    adapter = adapter_mod.APIServerAdapter(cfg)

    async def echo_handler(text: str, session_id: str) -> str:
        return f"echo: {text[-200:]}"

    adapter.set_handler(echo_handler)
    return adapter


@pytest.mark.asyncio
async def test_chain_via_previous_response_id(adapter_mod, monkeypatch):
    monkeypatch.setenv("API_SERVER_API_TYPE", "responses")
    adapter = _make_adapter(adapter_mod)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post("/v1/responses", json={"input": "what is 2+2"}, headers=_AUTH)
        body1 = await r1.json()
        prior_id = body1["id"]
        assert prior_id.startswith("resp-")

        r2 = await client.post(
            "/v1/responses",
            json={"input": "now add 5", "previous_response_id": prior_id},
            headers=_AUTH,
        )
        body2 = await r2.json()
        assert body2["previous_response_id"] == prior_id
        assistant_text = body2["output"][0]["content"][0]["text"]
        assert "what is 2+2" in assistant_text
        assert "now add 5" in assistant_text


@pytest.mark.asyncio
async def test_chain_via_named_conversation(adapter_mod, monkeypatch):
    monkeypatch.setenv("API_SERVER_API_TYPE", "responses")
    adapter = _make_adapter(adapter_mod)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post(
            "/v1/responses", json={"input": "first", "conversation": "p"}, headers=_AUTH
        )
        body1 = await r1.json()
        assert body1["conversation"] == "p"

        r2 = await client.post(
            "/v1/responses", json={"input": "second", "conversation": "p"}, headers=_AUTH
        )
        body2 = await r2.json()
        text = body2["output"][0]["content"][0]["text"]
        assert "first" in text and "second" in text


@pytest.mark.asyncio
async def test_unknown_previous_id_silent(adapter_mod, monkeypatch):
    monkeypatch.setenv("API_SERVER_API_TYPE", "responses")
    adapter = _make_adapter(adapter_mod)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            json={"input": "hi", "previous_response_id": "resp-bogus"},
            headers=_AUTH,
        )
        body = await r.json()
        assert "previous_response_id" not in body


@pytest.mark.asyncio
async def test_lru_eviction(adapter_mod, monkeypatch):
    monkeypatch.setenv("API_SERVER_API_TYPE", "responses")
    adapter = _make_adapter(adapter_mod)
    adapter._responses_max = 3
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        ids: list[str] = []
        for i in range(5):
            r = await client.post("/v1/responses", json={"input": f"t{i}"}, headers=_AUTH)
            ids.append((await r.json())["id"])
        assert len(adapter._responses_store) == 3
        assert ids[0] not in adapter._responses_store
        assert ids[-1] in adapter._responses_store


@pytest.mark.asyncio
async def test_capabilities_advertises_chaining(adapter_mod):
    adapter = _make_adapter(adapter_mod)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/v1/capabilities")
        body = await r.json()
        assert body["features"]["previous_response_id"] is True
