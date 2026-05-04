"""Tests for OpenRouter response-side cache-status capture.

Wave 5 T5 final closure (Hermes-port 457c7b76c). The provider installs
an httpx response hook that reads ``X-OpenRouter-Cache-Status`` from
every response and stashes it on the provider instance.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_or_provider():
    p = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "openrouter-provider"
        / "provider.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_or_provider_for_T5_response", str(p)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def or_mod():
    return _load_or_provider()


@pytest.fixture(autouse=True)
def _stub_api_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")


def test_initial_cache_status_is_miss(or_mod):
    p = or_mod.OpenRouterProvider()
    assert p.last_or_cache_status == "MISS"


def test_event_hook_registered_on_http_client(or_mod):
    p = or_mod.OpenRouterProvider()
    # AsyncOpenAI exposes the httpx client at p.client._client (private)
    # The custom http_client we passed should have our response hook.
    httpx_client = getattr(p.client, "_client", None)
    if httpx_client is None:
        pytest.skip("AsyncOpenAI internal layout differs; skipping introspection check")
    hooks = getattr(httpx_client, "event_hooks", {})
    response_hooks = hooks.get("response", [])
    assert len(response_hooks) >= 1


@pytest.mark.asyncio
async def test_hook_updates_cache_status_on_hit(or_mod):
    p = or_mod.OpenRouterProvider()
    fake_response = MagicMock(headers={"X-OpenRouter-Cache-Status": "HIT"})
    httpx_client = getattr(p.client, "_client", None)
    if httpx_client is None:
        pytest.skip("AsyncOpenAI internal layout differs")
    hooks = httpx_client.event_hooks.get("response", [])
    assert len(hooks) >= 1
    await hooks[0](fake_response)
    assert p.last_or_cache_status == "HIT"


@pytest.mark.asyncio
async def test_hook_updates_cache_status_on_miss(or_mod):
    p = or_mod.OpenRouterProvider()
    fake_response = MagicMock(headers={"X-OpenRouter-Cache-Status": "MISS"})
    httpx_client = getattr(p.client, "_client", None)
    if httpx_client is None:
        pytest.skip("AsyncOpenAI internal layout differs")
    hooks = httpx_client.event_hooks.get("response", [])
    await hooks[0](fake_response)
    assert p.last_or_cache_status == "MISS"


@pytest.mark.asyncio
async def test_hook_handles_missing_header_gracefully(or_mod):
    p = or_mod.OpenRouterProvider()
    fake_response = MagicMock(headers={})  # no cache-status header
    httpx_client = getattr(p.client, "_client", None)
    if httpx_client is None:
        pytest.skip("AsyncOpenAI internal layout differs")
    hooks = httpx_client.event_hooks.get("response", [])
    # parse_cache_status returns "MISS" on missing header
    await hooks[0](fake_response)
    assert p.last_or_cache_status == "MISS"


@pytest.mark.asyncio
async def test_hook_robust_to_response_exception(or_mod):
    p = or_mod.OpenRouterProvider()
    # Response object that raises when headers are accessed
    fake_response = MagicMock()
    type(fake_response).headers = property(
        lambda self: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    httpx_client = getattr(p.client, "_client", None)
    if httpx_client is None:
        pytest.skip("AsyncOpenAI internal layout differs")
    hooks = httpx_client.event_hooks.get("response", [])
    # Hook must not raise — observability is best-effort
    await hooks[0](fake_response)
    # Status unchanged (hook silently absorbed the error)
    assert p.last_or_cache_status == "MISS"
