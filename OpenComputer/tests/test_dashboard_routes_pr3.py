"""Tests for /api/v1/{profiles,plugins,skills,tools}/* — PR3."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opencomputer.dashboard.server import build_app


def _client() -> TestClient:
    return TestClient(build_app(enable_pty=False))


def test_profiles_list():
    resp = _client().get("/api/v1/profiles")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "active" in body


def test_profiles_create_validates_empty():
    resp = _client().post("/api/v1/profiles", json={"name": ""})
    assert resp.status_code == 422  # min_length=1 enforced


def test_profile_setup_command_404_when_missing():
    resp = _client().get("/api/v1/profiles/does-not-exist/setup-command")
    assert resp.status_code == 404


def test_plugins_list():
    resp = _client().get("/api/v1/plugins")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and "discovered" in body


def test_plugins_install_validates_body():
    resp = _client().post("/api/v1/plugins/install", json={})
    assert resp.status_code == 422


def test_skills_list():
    resp = _client().get("/api/v1/skills")
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_skills_search_rejects_empty_query():
    resp = _client().get("/api/v1/skills/search?q=")
    assert resp.status_code in (400, 422)


def test_skills_toggle_validates_body():
    resp = _client().put("/api/v1/skills/toggle", json={})
    assert resp.status_code == 422


def test_tools_toolsets():
    resp = _client().get("/api/v1/tools/toolsets")
    # Either populated registry (200) or unavailable (503) — both acceptable
    # in a unit-test where no agent loop has registered tools yet.
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        assert "items" in resp.json()
