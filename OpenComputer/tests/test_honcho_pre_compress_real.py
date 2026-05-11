"""Tests for HonchoSelfHostedProvider.on_pre_compress (the real impl).

Before 2026-05-11 this method returned ``None`` (TODO stub). The new
implementation calls ``/v1/context-full`` and returns a formatted
preservation block. These tests verify:

* Successful 200 response → formatted block returned
* HTTP error → None (compaction proceeds)
* Network exception → None
* Empty/short payload → None
* Closed client → None
* Truncation at 2000 chars cap
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

# extensions/memory-honcho/provider.py is loaded under a synthetic
# module name because the hyphenated directory name isn't a Python
# package. Mirrors the pattern in
# tests/test_honcho_param_uplift.py and friends.
_PROVIDER_PATH = (
    Path(__file__).resolve().parent.parent
    / "extensions"
    / "memory-honcho"
    / "provider.py"
)


def _load_provider_module():
    cache_key = "memory_honcho_provider_pre_compress_test"
    if cache_key in sys.modules:
        return sys.modules[cache_key]
    spec = importlib.util.spec_from_file_location(cache_key, _PROVIDER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[cache_key] = mod
    spec.loader.exec_module(mod)
    return mod


_provider_mod = _load_provider_module()
HonchoConfig = _provider_mod.HonchoConfig
HonchoSelfHostedProvider = _provider_mod.HonchoSelfHostedProvider


def _provider(handler) -> HonchoSelfHostedProvider:
    """Build a provider whose httpx client uses the given MockTransport handler."""
    cfg = HonchoConfig(base_url="http://test.local", host_key="test")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=cfg.base_url,
    )
    return HonchoSelfHostedProvider(config=cfg, http_client=client)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if asyncio.get_event_loop().is_running() else asyncio.run(coro)


def test_pre_compress_returns_formatted_block_on_200():
    """Happy path: 200 + payload → wrapped pinned block returned."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/context-full"
        return httpx.Response(
            200,
            json={
                "context": "User prefers concise replies. "
                "Lives in Tokyo. Working on stocks."
            },
        )

    provider = _provider(handler)
    result = asyncio.run(provider.on_pre_compress([]))
    assert result is not None
    assert "User prefers concise replies" in result
    assert result.startswith("## Honcho user-model facts")


def test_pre_compress_returns_none_on_4xx():
    """4xx → None so compaction proceeds without injecting noise."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    provider = _provider(handler)
    result = asyncio.run(provider.on_pre_compress([]))
    assert result is None


def test_pre_compress_returns_none_on_network_error():
    """Connection error → None (logged, never raised)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = _provider(handler)
    result = asyncio.run(provider.on_pre_compress([]))
    assert result is None


def test_pre_compress_returns_none_on_empty_payload():
    """Empty string payload → None (no signal to preserve)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"context": ""})

    provider = _provider(handler)
    result = asyncio.run(provider.on_pre_compress([]))
    assert result is None


def test_pre_compress_returns_none_on_short_payload():
    """Payload shorter than 16 chars → None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"context": "short"})

    provider = _provider(handler)
    result = asyncio.run(provider.on_pre_compress([]))
    assert result is None


def test_pre_compress_returns_none_on_closed_client():
    """Client closed during shutdown race → None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"context": "ok ok ok ok"})

    provider = _provider(handler)
    asyncio.run(provider._client.aclose())
    result = asyncio.run(provider.on_pre_compress([]))
    assert result is None


def test_pre_compress_caps_at_2000_chars():
    """Very long Honcho payloads are truncated."""
    long_text = "fact about user " * 500  # ~ 8000 chars

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"context": long_text})

    provider = _provider(handler)
    result = asyncio.run(provider.on_pre_compress([]))
    assert result is not None
    # Header is ~50 chars; body capped at 2000 chars.
    body = result.split("\n\n", 1)[1]
    assert len(body) <= 2000


def test_pre_compress_handles_malformed_json():
    """Non-JSON 200 response → None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>not json</html>",
            headers={"content-type": "text/html"},
        )

    provider = _provider(handler)
    result = asyncio.run(provider.on_pre_compress([]))
    assert result is None
