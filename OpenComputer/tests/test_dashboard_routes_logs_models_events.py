"""Tests for /api/v1/{logs,models,events}."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from opencomputer.dashboard.server import build_app


def test_models_lists_providers():
    app = build_app(enable_pty=False)
    resp = TestClient(app).get("/api/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert "providers" in body
    assert isinstance(body["providers"], list)
    # At least anthropic + openai should be there
    names = {p["provider"] for p in body["providers"]}
    assert "anthropic" in names
    assert "openai" in names


def test_models_info_returns_provider_and_model():
    app = build_app(enable_pty=False)
    resp = TestClient(app).get("/api/v1/models/info")
    assert resp.status_code == 200
    body = resp.json()
    assert "provider" in body
    assert "model" in body


def test_models_set_validates():
    app = build_app(enable_pty=False)
    resp = TestClient(app).post("/api/v1/models/set", json={})
    # Pydantic validation error → 422
    assert resp.status_code == 422


def test_logs_recent_returns_buffer():
    app = build_app(enable_pty=False)
    # Trigger a log entry that lands in the dashboard handler
    logging.getLogger("opencomputer.test").warning("dashboard-test-log-line-XYZ")
    resp = TestClient(app).get("/api/v1/logs/recent?limit=20")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and isinstance(body["items"], list)
    msgs = [e["msg"] for e in body["items"]]
    assert any("dashboard-test-log-line-XYZ" in m for m in msgs)


def test_logs_recent_filters_by_level():
    app = build_app(enable_pty=False)
    logging.getLogger("opencomputer.test").error("err-marker-only")
    logging.getLogger("opencomputer.test").debug("debug-marker-only")
    resp = TestClient(app).get("/api/v1/logs/recent?level=ERROR&limit=20")
    assert resp.status_code == 200
    msgs = [e["msg"] for e in resp.json()["items"]]
    assert any("err-marker-only" in m for m in msgs)
    # debug message is filtered out
    assert not any("debug-marker-only" in m for m in msgs)


def test_events_endpoint_route_exists():
    """Smoke check that the events route is wired without spinning up
    the streaming generator (TestClient.stream against an infinite SSE
    handler hangs)."""
    app = build_app(enable_pty=False)
    # Routes should include /api/v1/events
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/api/v1/events" in paths
