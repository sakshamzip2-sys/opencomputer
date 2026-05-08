"""T58 — `event: hermes.tool.progress` SSE in /v1/chat/completions streaming."""

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


@pytest.mark.asyncio
async def test_v2_handler_emits_tool_progress_sse(adapter_mod):
    cfg = {"host": "127.0.0.1", "port": 0, "token": "tok"}
    adapter = adapter_mod.APIServerAdapter(cfg)

    async def v2_handler(session_id: str, text: str, hooks):
        await hooks.emit_tool_progress("grep", "running", "scanning files…")
        await hooks.emit_text("found ")
        await hooks.emit_tool_progress("grep", "done", "12 matches")
        await hooks.emit_text("12 matches.")

    adapter.set_streaming_handler_v2(v2_handler)

    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "x",
                "messages": [{"role": "user", "content": "find todos"}],
                "stream": True,
            },
            headers=_AUTH,
        )
        assert r.status == 200
        body = await r.text()
        # Two tool-progress events with the canonical SSE event: name.
        assert body.count("event: hermes.tool.progress") == 2
        assert "running" in body
        assert "scanning" in body
        assert "12 matches" in body


@pytest.mark.asyncio
async def test_v1_handler_still_works_without_progress(adapter_mod):
    """Backwards compat: V1 handler (just on_delta) keeps working."""
    cfg = {"host": "127.0.0.1", "port": 0, "token": "tok"}
    adapter = adapter_mod.APIServerAdapter(cfg)

    async def v1_handler(session_id: str, text: str, on_delta):
        await on_delta("hello")

    adapter.set_streaming_handler(v1_handler)

    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers=_AUTH,
        )
        body = await r.text()
        assert "hermes.tool.progress" not in body  # V1 doesn't emit
        assert "hello" in body


@pytest.mark.asyncio
async def test_v2_takes_precedence_over_v1(adapter_mod):
    cfg = {"host": "127.0.0.1", "port": 0, "token": "tok"}
    adapter = adapter_mod.APIServerAdapter(cfg)

    v1_called = {"n": 0}

    async def v1_handler(session_id: str, text: str, on_delta):
        v1_called["n"] += 1
        await on_delta("v1")

    async def v2_handler(session_id: str, text: str, hooks):
        await hooks.emit_text("v2")

    adapter.set_streaming_handler(v1_handler)
    adapter.set_streaming_handler_v2(v2_handler)

    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers=_AUTH,
        )
        body = await r.text()
        assert "v2" in body
        assert v1_called["n"] == 0  # V1 never invoked when V2 is set
