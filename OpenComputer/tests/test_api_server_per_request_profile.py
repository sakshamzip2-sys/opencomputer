"""T61 — per-request profile via X-OC-Profile header."""

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


def _adapter(adapter_mod):
    cfg = {"host": "127.0.0.1", "port": 0, "token": "tok"}
    return adapter_mod.APIServerAdapter(cfg)


@pytest.mark.asyncio
async def test_capabilities_honors_x_oc_profile(adapter_mod, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "default")
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/v1/capabilities", headers={"X-OC-Profile": "alice"})
        body = await r.json()
        assert body["profile"] == "alice"


@pytest.mark.asyncio
async def test_capabilities_falls_back_to_env(adapter_mod, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "bob")
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/v1/capabilities")
        body = await r.json()
        assert body["profile"] == "bob"


@pytest.mark.asyncio
async def test_invalid_profile_silently_ignored(adapter_mod, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "default")
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get(
            "/v1/capabilities", headers={"X-OC-Profile": "../etc/passwd"}
        )
        body = await r.json()
        # Path-traversal-y profile rejected; falls back.
        assert body["profile"] == "default"


@pytest.mark.asyncio
async def test_health_detailed_honors_profile(adapter_mod, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "default")
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get(
            "/health/detailed", headers={"X-OC-Profile": "carol"}
        )
        body = await r.json()
        assert body["api_server"]["profile"] == "carol"


@pytest.mark.asyncio
async def test_resolve_helper_returns_none_when_no_header(adapter_mod):
    from aiohttp import web

    a = _adapter(adapter_mod)
    # Build a fake Request-like object with empty headers.
    class FakeReq:
        headers: dict = {}

    assert a._resolve_request_profile(FakeReq()) is None
