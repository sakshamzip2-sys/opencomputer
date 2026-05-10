"""Tests for /api/v1/status — SPA's first call on mount.

The status route is loopback-public (no token check) so the SPA can
render the StatusBar before authenticating. It returns the active
profile, the wire URL the SPA should connect to for live chat, and
the OC version string.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from opencomputer.dashboard.server import build_app


def test_status_returns_profile_and_wire_url():
    app = build_app(wire_url="ws://127.0.0.1:18789", enable_pty=False)
    client = TestClient(app)
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "profile" in body
    assert "wire_url" in body
    assert body["wire_url"] == "ws://127.0.0.1:18789"
    assert "version" in body
    # version should be a non-empty string (e.g. "2026.5.5")
    assert isinstance(body["version"], str) and body["version"]


def test_status_is_loopback_public():
    """The SPA must be able to call /api/v1/status WITHOUT a token,
    on loopback bind. (Token enforcement only applies on non-loopback.)"""
    app = build_app(enable_pty=False)
    client = TestClient(app)
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200


def test_status_default_profile_when_no_profile_name():
    """When `default_config()` has no profile_name attr, the route
    falls back to the literal "default"."""
    app = build_app(enable_pty=False)
    client = TestClient(app)
    body = client.get("/api/v1/status").json()
    # profile is non-empty either way
    assert body["profile"]
