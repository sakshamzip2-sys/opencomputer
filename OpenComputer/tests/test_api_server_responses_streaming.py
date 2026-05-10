"""T58 — `/v1/responses` SSE streaming with `event: hermes.tool.progress`."""

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

    async def handler(text: str, session_id: str) -> str:
        return f"echo: {text[-40:]}"

    adapter.set_handler(handler)
    return adapter


@pytest.mark.asyncio
async def test_responses_streaming_emits_tool_progress(adapter_mod, monkeypatch):
    monkeypatch.setenv("API_SERVER_API_TYPE", "responses")
    adapter = _make_adapter(adapter_mod)

    async def v2_handler(session_id, text, hooks):
        await hooks.emit_tool_progress("WebSearch", "running", "querying…")
        await hooks.emit_text("Found ")
        await hooks.emit_tool_progress("WebSearch", "done", "3 hits")
        await hooks.emit_text("3 results.")

    adapter.set_streaming_handler_v2(v2_handler)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            json={"input": "hi", "stream": True},
            headers=_AUTH,
        )
        assert r.status == 200
        body = await r.text()
        assert "event: response.created" in body
        assert body.count("event: hermes.tool.progress") == 2
        assert "WebSearch" in body
        assert "querying" in body
        assert "Found " in body
        assert "3 results." in body
        assert "event: response.completed" in body


@pytest.mark.asyncio
async def test_responses_streaming_without_v2_uses_text_only(adapter_mod, monkeypatch):
    """Falls back to single-shot output when no V2 handler is set."""
    monkeypatch.setenv("API_SERVER_API_TYPE", "responses")
    adapter = _make_adapter(adapter_mod)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            json={"input": "ping", "stream": True},
            headers=_AUTH,
        )
        body = await r.text()
        assert "event: response.created" in body
        assert "event: response.completed" in body
        assert "echo: " in body
        assert "hermes.tool.progress" not in body


@pytest.mark.asyncio
async def test_responses_non_streaming_still_works(adapter_mod, monkeypatch):
    monkeypatch.setenv("API_SERVER_API_TYPE", "responses")
    adapter = _make_adapter(adapter_mod)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses", json={"input": "ping"}, headers=_AUTH
        )
        body = await r.json()
        assert body["id"].startswith("resp-")


@pytest.mark.asyncio
async def test_capabilities_advertises_tool_progress(adapter_mod):
    adapter = _make_adapter(adapter_mod)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/v1/capabilities")
        body = await r.json()
        assert body["features"]["tool_progress"] is True
