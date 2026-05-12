"""Smoke test for the ``/health`` alias added 2026-05-12.

Hermes-workspace's gateway-capabilities probe hits the bare ``/health``
path (not ``/api/health``). Without the alias the workspace would
render the dashboard as "disconnected" even though OC is serving every
other endpoint.

This test guards against accidentally removing the alias (or shadowing
it with a route registered earlier).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from opencomputer.dashboard.server import build_app


def test_bare_health_returns_ok() -> None:
    app = build_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert "wire_url" in body


def test_api_health_still_works() -> None:
    """The legacy /api/health endpoint must still respond — many SPAs
    and scripts rely on it."""
    app = build_app()
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_v1_health_still_works() -> None:
    """The OpenAI-compat /v1/health endpoint must keep its own shape."""
    app = build_app()
    client = TestClient(app)
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
