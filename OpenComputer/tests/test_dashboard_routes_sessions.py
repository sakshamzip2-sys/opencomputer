"""Tests for /api/v1/sessions/* — list/get/messages/search/delete."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencomputer.agent.state import SessionDB
from opencomputer.dashboard.server import build_app
from plugin_sdk.core import Message


@pytest.fixture
def populated_db(tmp_path: Path, monkeypatch) -> Path:
    """Spin up a real SessionDB at a tmp path and populate it. Redirects
    `default_config().home` via OPENCOMPUTER_HOME so the routes find it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(home))

    db_path = home / "sessions.db"
    db = SessionDB(db_path)
    db.create_session(
        session_id="ses-1",
        platform="cli",
        model="claude-sonnet-4-6",
        title="Hello world conversation",
    )
    db.append_message(
        "ses-1",
        Message(role="user", content="hello world"),
    )
    db.append_message(
        "ses-1",
        Message(role="assistant", content="hi there"),
    )
    db.create_session(
        session_id="ses-2",
        platform="telegram",
        model="claude-sonnet-4-6",
        title="Another session",
    )
    db.append_message(
        "ses-2",
        Message(role="user", content="another message"),
    )
    return db_path


@pytest.fixture
def client(populated_db) -> TestClient:
    return TestClient(build_app(enable_pty=False))


def test_list_sessions_returns_summary(client: TestClient):
    resp = client.get("/api/v1/sessions?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and isinstance(body["items"], list)
    assert len(body["items"]) >= 2
    # Find ses-1
    ids = {r["id"] for r in body["items"]}
    assert "ses-1" in ids and "ses-2" in ids


def test_list_sessions_rejects_above_max(client: TestClient):
    """Server-side Query bound rejects limit>200 (422). Belt-and-braces:
    clamp_limit() also caps inside the route in case the Query bound is
    ever loosened."""
    resp = client.get("/api/v1/sessions?limit=99999")
    assert resp.status_code == 422


def test_list_sessions_caps_at_max(client: TestClient):
    """Limit at the max value works."""
    resp = client.get("/api/v1/sessions?limit=200")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) <= 200


def test_list_sessions_filter_by_channel(client: TestClient):
    resp = client.get("/api/v1/sessions?channel=telegram")
    assert resp.status_code == 200
    rows = resp.json()["items"]
    assert all(r["platform"] == "telegram" for r in rows)
    assert any(r["id"] == "ses-2" for r in rows)


def test_get_session_returns_metadata(client: TestClient):
    resp = client.get("/api/v1/sessions/ses-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "ses-1"
    assert body["platform"] == "cli"


def test_get_session_404_when_missing(client: TestClient):
    resp = client.get("/api/v1/sessions/does-not-exist")
    assert resp.status_code == 404
    assert "session not found" in resp.json()["detail"]


def test_get_messages_returns_list(client: TestClient):
    resp = client.get("/api/v1/sessions/ses-1/messages?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert len(body["items"]) == 2
    # Items should preserve role
    roles = [m["role"] for m in body["items"]]
    assert "user" in roles and "assistant" in roles


def test_get_messages_paginates(client: TestClient):
    resp = client.get("/api/v1/sessions/ses-1/messages?limit=1&offset=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["seq"] == 1
    assert body["total"] == 2


def test_get_messages_404_when_session_missing(client: TestClient):
    resp = client.get("/api/v1/sessions/does-not-exist/messages")
    assert resp.status_code == 404


def test_search_returns_fts_matches(client: TestClient):
    resp = client.get("/api/v1/sessions/search?q=hello")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    contents = [m.get("content", "") for m in body["items"]]
    assert any("hello" in c for c in contents)


def test_search_rejects_empty_query(client: TestClient):
    # FastAPI's Query(..., min_length=1) returns 422 for empty string
    resp = client.get("/api/v1/sessions/search?q=")
    assert resp.status_code in (400, 422)


def test_delete_session_removes_row(client: TestClient):
    resp = client.delete("/api/v1/sessions/ses-1")
    assert resp.status_code == 204
    resp2 = client.get("/api/v1/sessions/ses-1")
    assert resp2.status_code == 404


def test_delete_nonexistent_session_404(client: TestClient):
    resp = client.delete("/api/v1/sessions/never-existed")
    assert resp.status_code == 404
