"""T60 — /api/jobs Jobs API (cron remote management)."""

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


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    yield tmp_path


_AUTH = {"Authorization": "Bearer tok"}


def _adapter(adapter_mod):
    cfg = {"host": "127.0.0.1", "port": 0, "token": "tok"}
    return adapter_mod.APIServerAdapter(cfg)


@pytest.mark.asyncio
async def test_create_then_list_then_get(adapter_mod):
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post(
            "/api/jobs",
            json={"schedule": "every 1h", "name": "x", "prompt": "hi"},
            headers=_AUTH,
        )
        assert r1.status == 201
        job = await r1.json()
        job_id = job["id"]

        r2 = await client.get("/api/jobs", headers=_AUTH)
        assert r2.status == 200
        body = await r2.json()
        assert any(j["id"] == job_id for j in body["jobs"])

        r3 = await client.get(f"/api/jobs/{job_id}", headers=_AUTH)
        assert r3.status == 200
        assert (await r3.json())["id"] == job_id


@pytest.mark.asyncio
async def test_create_without_schedule_400(adapter_mod):
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/api/jobs", json={}, headers=_AUTH)
        assert r.status == 400


@pytest.mark.asyncio
async def test_unauth_401(adapter_mod):
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/api/jobs")  # no Authorization
        assert r.status == 401


@pytest.mark.asyncio
async def test_pause_resume(adapter_mod):
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post(
            "/api/jobs",
            json={"schedule": "every 1h", "prompt": "hi"},
            headers=_AUTH,
        )
        job_id = (await r1.json())["id"]

        r2 = await client.post(
            f"/api/jobs/{job_id}/pause", json={"reason": "test"}, headers=_AUTH
        )
        assert r2.status == 200
        assert (await r2.json())["enabled"] is False

        r3 = await client.post(f"/api/jobs/{job_id}/resume", headers=_AUTH)
        assert r3.status == 200
        assert (await r3.json())["enabled"] is True


@pytest.mark.asyncio
async def test_patch_updates_fields(adapter_mod):
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post(
            "/api/jobs",
            json={"schedule": "every 1h", "prompt": "hi"},
            headers=_AUTH,
        )
        job_id = (await r1.json())["id"]

        r2 = await client.patch(
            f"/api/jobs/{job_id}",
            json={"name": "renamed"},
            headers=_AUTH,
        )
        assert r2.status == 200
        assert (await r2.json())["name"] == "renamed"


@pytest.mark.asyncio
async def test_delete(adapter_mod):
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post(
            "/api/jobs",
            json={"schedule": "every 1h", "prompt": "hi"},
            headers=_AUTH,
        )
        job_id = (await r1.json())["id"]

        r2 = await client.delete(f"/api/jobs/{job_id}", headers=_AUTH)
        assert r2.status == 200
        assert (await r2.json())["deleted"] is True

        r3 = await client.get(f"/api/jobs/{job_id}", headers=_AUTH)
        assert r3.status == 404


@pytest.mark.asyncio
async def test_unknown_id_404(adapter_mod):
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        for path in (
            "/api/jobs/nope",
            "/api/jobs/nope/pause",
            "/api/jobs/nope/resume",
            "/api/jobs/nope/run",
        ):
            method = "post" if path != "/api/jobs/nope" else "get"
            r = await getattr(client, method)(path, headers=_AUTH)
            assert r.status == 404


@pytest.mark.asyncio
async def test_capabilities_advertises_jobs_api(adapter_mod):
    a = _adapter(adapter_mod)
    app = a._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/v1/capabilities")
        body = await r.json()
        assert body["features"]["jobs_api"] is True
