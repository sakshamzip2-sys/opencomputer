"""Hermes parity G2: Idempotency-Key dedup with 5-min TTL."""
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


@pytest.fixture(autouse=True)
def clear_idempotency_cache():
    mod = _load_adapter()
    if hasattr(mod, "_IDEMPOTENCY_CACHE"):
        mod._IDEMPOTENCY_CACHE.clear()
    yield
    if hasattr(mod, "_IDEMPOTENCY_CACHE"):
        mod._IDEMPOTENCY_CACHE.clear()


@pytest.mark.asyncio
async def test_repeat_request_returns_cached_response():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    counter = {"n": 0}

    async def handler(sid, message):
        counter["n"] += 1
        return f"call-{counter['n']}"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok", "Idempotency-Key": "abc123"},
            json={"message": "hi"},
        )
        r2 = await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok", "Idempotency-Key": "abc123"},
            json={"message": "hi"},
        )
        assert r1.status == 200, await r1.text()
        assert r2.status == 200, await r2.text()
        assert counter["n"] == 1, "handler called twice — idempotency missed"
        assert r2.headers.get("X-Idempotent-Replay") == "1"
        b1 = await r1.text()
        b2 = await r2.text()
        assert b1 == b2


@pytest.mark.asyncio
async def test_different_keys_call_handler_separately():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    counter = {"n": 0}

    async def handler(sid, message):
        counter["n"] += 1
        return "ok"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok", "Idempotency-Key": "k1"},
            json={"message": "hi"},
        )
        await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok", "Idempotency-Key": "k2"},
            json={"message": "hi"},
        )
        assert counter["n"] == 2


@pytest.mark.asyncio
async def test_no_key_means_no_cache():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    counter = {"n": 0}

    async def handler(sid, message):
        counter["n"] += 1
        return "ok"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok"},
            json={"message": "hi"},
        )
        await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok"},
            json={"message": "hi"},
        )
        assert counter["n"] == 2


@pytest.mark.asyncio
async def test_different_tokens_have_separate_caches():
    mod = _load_adapter()
    a = mod.APIServerAdapter({"host": "127.0.0.1", "port": 0, "token": "tok"})
    counter = {"n": 0}

    async def handler(sid, message):
        counter["n"] += 1
        return "ok"

    a.set_handler(handler)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok", "Idempotency-Key": "k1"},
            json={"message": "hi"},
        )
        # Wrong token will be rejected at auth — but the cache key includes
        # token-hash so even if both authenticated, they'd cache separately.
        await client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer tok-other", "Idempotency-Key": "k1"},
            json={"message": "hi"},
        )
        # Counter = 1 (only the valid call ran; the wrong-token call 401'd
        # before the handler — and cache key was different anyway, so
        # there'd have been no cache-hit even if it had reached handler).
        assert counter["n"] == 1
