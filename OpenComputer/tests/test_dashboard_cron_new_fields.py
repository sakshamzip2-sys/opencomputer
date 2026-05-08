"""Verify the dashboard /api/v1/cron/jobs/* GET endpoints surface
all Hermes-parity fields added 2026-05-08/09: skills, origin_*, notify,
plan_mode, enabled_toolsets, context_from, workdir, no_agent, script.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from opencomputer.cron.jobs import create_job


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture
def client():
    from fastapi import FastAPI

    from opencomputer.dashboard.routes import cron as cron_routes
    app = FastAPI()
    app.include_router(cron_routes.router)
    return TestClient(app)


def test_list_surfaces_skills(client):
    create_job(schedule="every 1h", skills=["a", "b"], notify="local")
    resp = client.get("/api/v1/cron/jobs")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["skills"] == ["a", "b"]
    assert item["notify"] == "local"


def test_get_surfaces_origin_fields(client):
    job = create_job(
        schedule="every 1h",
        skill="x",
        notify="origin",
        origin_platform="telegram",
        origin_chat_id="-100123",
        origin_thread_id="17585",
    )
    resp = client.get(f"/api/v1/cron/jobs/{job['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["origin_platform"] == "telegram"
    assert body["origin_chat_id"] == "-100123"
    assert body["origin_thread_id"] == "17585"
    assert body["notify"] == "origin"


def test_get_surfaces_runtime_fields(client):
    job = create_job(
        schedule="every 1h",
        skill="x",
        enabled_toolsets=["Read", "Grep"],
        plan_mode=False,
        workdir="/tmp/work",
    )
    resp = client.get(f"/api/v1/cron/jobs/{job['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled_toolsets"] == ["Read", "Grep"]
    assert body["plan_mode"] is False
    assert body["workdir"] == "/tmp/work"


def test_get_no_agent_script_fields(client):
    job = create_job(
        schedule="every 5m",
        no_agent=True,
        script="watchdog.sh",
        script_timeout_seconds=60,
    )
    resp = client.get(f"/api/v1/cron/jobs/{job['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["no_agent"] is True
    assert body["script"] == "watchdog.sh"
