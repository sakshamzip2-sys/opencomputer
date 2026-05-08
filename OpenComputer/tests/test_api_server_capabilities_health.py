"""T2 + T3 — API server `/v1/capabilities` + `/health/detailed`.

Hermes-doc parity:
- ``GET /v1/capabilities`` returns a machine-readable feature flag dict
  for integrators (no auth required).
- ``GET /health/detailed`` returns sessions / agents / uptime / memory
  fields. Never returns 5xx; partial failures surface as null fields.

Adapter is loaded by file path (matches existing
``test_api_server_openai_compat.py`` pattern) because the directory
``extensions/api-server/`` has a hyphen and isn't a package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer


def _load_adapter_module():
    """Load extensions/api-server/adapter.py by path (hyphenated dir)."""
    spec_path = (
        Path(__file__).parent.parent
        / "extensions"
        / "api-server"
        / "adapter.py"
    )
    if "api_server_adapter_test" in sys.modules:
        return sys.modules["api_server_adapter_test"]
    spec = importlib.util.spec_from_file_location(
        "api_server_adapter_test", spec_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def adapter_mod():
    return _load_adapter_module()


def _make_adapter(adapter_mod, *, token: str = ""):
    cfg = {"host": "127.0.0.1", "port": 0, "token": token}
    return adapter_mod.APIServerAdapter(cfg)


@pytest.mark.asyncio
async def test_capabilities_returns_feature_dict(adapter_mod):
    adapter = _make_adapter(adapter_mod)
    app = adapter._build_app()
    server = TestServer(app)
    async with TestClient(server) as client:
        resp = await client.get("/v1/capabilities")
        assert resp.status == 200
        payload = await resp.json()
        assert payload["version"] == "1"
        features = payload["features"]
        assert features["chat_completions"] is True
        assert features["streaming"] is True
        assert features["tool_calls"] is True
        # T57 — chaining shipped; runs/jobs honestly deferred.
        assert features["previous_response_id"] is True
        assert features["runs_api"] is False
        assert features["jobs_api"] is False


@pytest.mark.asyncio
async def test_capabilities_no_auth_required(adapter_mod):
    """Capabilities is public — no Bearer token needed (matches Hermes spec)."""
    adapter = _make_adapter(adapter_mod, token="secret-token-not-sent")
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/capabilities")  # no Authorization header
        assert resp.status == 200


@pytest.mark.asyncio
async def test_capabilities_advertises_profile(adapter_mod, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "alice")
    adapter = _make_adapter(adapter_mod)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/capabilities")
        payload = await resp.json()
        assert payload["profile"] == "alice"
        assert payload["model"] == "alice"


@pytest.mark.asyncio
async def test_health_detailed_returns_status_ok(adapter_mod):
    adapter = _make_adapter(adapter_mod)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health/detailed")
        assert resp.status == 200
        payload = await resp.json()
        assert payload["status"] == "ok"
        assert "uptime_seconds" in payload
        assert "running_agents" in payload
        # api_server section always present
        assert payload["api_server"]["host"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_health_detailed_partial_failure_returns_200(adapter_mod, monkeypatch):
    """Failures in sub-lookups surface as null fields, not 5xx."""

    def boom():
        raise RuntimeError("simulated")

    monkeypatch.setattr(adapter_mod, "_count_active_sessions", boom)
    monkeypatch.setattr(adapter_mod, "_count_total_sessions", boom)
    monkeypatch.setattr(adapter_mod, "_process_memory_mb", boom)

    adapter = _make_adapter(adapter_mod)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health/detailed")
        assert resp.status == 200
        payload = await resp.json()
        # status still ok despite null fields
        assert payload["status"] == "ok"
        assert payload["sessions"] is None
        assert payload["memory_mb"] is None


@pytest.mark.asyncio
async def test_health_detailed_no_auth_required(adapter_mod):
    """Health detailed is public for monitoring agents."""
    adapter = _make_adapter(adapter_mod, token="secret")
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health/detailed")  # no Authorization
        assert resp.status == 200
