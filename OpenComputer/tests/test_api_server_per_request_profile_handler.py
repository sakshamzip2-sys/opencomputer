"""T61 hot-path — X-OC-Profile threaded into handlers via ContextVar.

The advertisement surfaces (capabilities/health/models) already honor
the X-OC-Profile header. This file covers the *hot path* — the actual
handler invocation for /v1/chat/completions and /v1/responses sees the
profile via ``get_current_request_profile()`` so multi-tenant routers
can dispatch to the right profile's memory/plugins/MCP.
"""

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


def _make_adapter(adapter_mod, *, capture: dict):
    cfg = {"host": "127.0.0.1", "port": 0, "token": "tok"}
    adapter = adapter_mod.APIServerAdapter(cfg)

    async def handler(text: str, session_id: str) -> str:
        capture["sync_profile"] = adapter_mod.get_current_request_profile()
        return "ok"

    async def streaming_v2(session_id, text, hooks):
        capture["v2_profile"] = adapter_mod.get_current_request_profile()
        await hooks.emit_text("ok")

    adapter.set_handler(handler)
    adapter.set_streaming_handler_v2(streaming_v2)
    return adapter


@pytest.mark.asyncio
async def test_sync_handler_sees_per_request_profile(adapter_mod):
    cap: dict = {}
    adapter = _make_adapter(adapter_mod, capture=cap)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
            headers={**_AUTH, "X-OC-Profile": "alice"},
        )
        assert r.status == 200
        assert cap["sync_profile"] == "alice"


@pytest.mark.asyncio
async def test_streaming_handler_sees_per_request_profile(adapter_mod):
    cap: dict = {}
    adapter = _make_adapter(adapter_mod, capture=cap)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/chat/completions",
            json={
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers={**_AUTH, "X-OC-Profile": "bob"},
        )
        assert r.status == 200
        await r.text()
        assert cap["v2_profile"] == "bob"


@pytest.mark.asyncio
async def test_responses_handler_sees_per_request_profile(adapter_mod, monkeypatch):
    monkeypatch.setenv("API_SERVER_API_TYPE", "responses")
    cap: dict = {}
    adapter = _make_adapter(adapter_mod, capture=cap)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/v1/responses",
            json={"input": "hi"},
            headers={**_AUTH, "X-OC-Profile": "carol"},
        )
        assert r.status == 200
        assert cap["sync_profile"] == "carol"


@pytest.mark.asyncio
async def test_no_header_means_none(adapter_mod):
    cap: dict = {}
    adapter = _make_adapter(adapter_mod, capture=cap)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
            headers=_AUTH,
        )
        assert cap["sync_profile"] is None


@pytest.mark.asyncio
async def test_invalid_header_value_silently_ignored(adapter_mod):
    """Path-traversal-y values get rejected to None."""
    cap: dict = {}
    adapter = _make_adapter(adapter_mod, capture=cap)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
            headers={**_AUTH, "X-OC-Profile": "../etc/passwd"},
        )
        assert cap["sync_profile"] is None


@pytest.mark.asyncio
async def test_concurrent_requests_dont_leak_profile(adapter_mod):
    """ContextVar isolation: two concurrent requests see distinct profiles."""
    import asyncio

    cap_a: dict = {}
    cap_b: dict = {}

    cfg = {"host": "127.0.0.1", "port": 0, "token": "tok"}
    adapter = adapter_mod.APIServerAdapter(cfg)
    barrier = asyncio.Event()
    seen = {"alice": False, "bob": False}

    async def handler(text: str, session_id: str) -> str:
        prof = adapter_mod.get_current_request_profile()
        seen[prof] = True
        if not all(seen.values()):
            try:
                await asyncio.wait_for(barrier.wait(), timeout=2.0)
            except TimeoutError:
                pass
        else:
            barrier.set()
        # Re-read AFTER the await to confirm ContextVar copies.
        if prof == "alice":
            cap_a["after_await"] = adapter_mod.get_current_request_profile()
        else:
            cap_b["after_await"] = adapter_mod.get_current_request_profile()
        return "ok"

    adapter.set_handler(handler)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        ra, rb = await asyncio.gather(
            client.post(
                "/v1/chat/completions",
                json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                headers={**_AUTH, "X-OC-Profile": "alice"},
            ),
            client.post(
                "/v1/chat/completions",
                json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                headers={**_AUTH, "X-OC-Profile": "bob"},
            ),
        )
        assert ra.status == 200
        assert rb.status == 200
        assert cap_a["after_await"] == "alice"
        assert cap_b["after_await"] == "bob"
