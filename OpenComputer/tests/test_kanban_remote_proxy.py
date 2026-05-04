"""Tests for the kanban remote-read proxy + client (Wave 6.E.11)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from opencomputer.dashboard import build_app
from opencomputer.kanban import db
from opencomputer.kanban.remote_client import (
    RemoteKanbanClient,
    RemoteKanbanError,
)


@pytest.fixture()
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("OC_KANBAN_DB", raising=False)
    monkeypatch.delenv("OC_KANBAN_BOARD", raising=False)
    db.init_db()
    return tmp_path


@pytest.fixture()
def client(kanban_home: Path) -> TestClient:
    app = build_app(enable_pty=False)
    c = TestClient(app)
    c._token = app.state.session_token
    return c


# ---- proxy routes ----


def test_proxy_health_returns_envelope(client: TestClient):
    r = client.get("/api/plugins/kanban/proxy/health")
    assert r.status_code == 200
    body = r.json()
    assert body["schema_version"] == 1
    assert "boards" in body
    assert "active_board" in body


def test_proxy_board_default_works(client: TestClient, kanban_home: Path):
    # Plant a task in default board
    with db.connect() as conn:
        db.create_task(conn, title="hello", body=None, assignee="me")
    r = client.get("/api/plugins/kanban/proxy/board")
    assert r.status_code == 200
    body = r.json()
    assert body["schema_version"] == 1
    titles = [t["title"] for t in body["tasks"]]
    assert "hello" in titles


def test_proxy_board_with_slug(client: TestClient, kanban_home: Path):
    # Create a named board + task in it
    target = db.board_db_path("alpha")
    target.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(db_path=target)
    with db.connect(target) as conn:
        db.create_task(conn, title="from-alpha", body=None, assignee="x")
    r = client.get("/api/plugins/kanban/proxy/board?slug=alpha")
    assert r.status_code == 200
    titles = [t["title"] for t in r.json()["tasks"]]
    assert "from-alpha" in titles


def test_proxy_board_unknown_slug_404(client: TestClient):
    r = client.get("/api/plugins/kanban/proxy/board?slug=nope")
    assert r.status_code == 404


def test_proxy_board_invalid_slug_400(client: TestClient):
    r = client.get("/api/plugins/kanban/proxy/board?slug=Bad+Slug")
    assert r.status_code == 400


def test_proxy_task_returns_full_view(client: TestClient, kanban_home: Path):
    with db.connect() as conn:
        tid = db.create_task(conn, title="single", body="body text", assignee="me")
        # Add a comment
        db.add_comment(conn, task_id=tid, author="me", body="hi")
    r = client.get(f"/api/plugins/kanban/proxy/task/{tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["task"]["title"] == "single"
    assert len(body["comments"]) == 1
    assert body["comments"][0]["body"] == "hi"


def test_proxy_task_unknown_404(client: TestClient):
    r = client.get("/api/plugins/kanban/proxy/task/totally-fake")
    assert r.status_code == 404


# ---- client ----


def test_client_health_round_trip():
    """End-to-end via httpx mock."""
    fake = httpx.Response(
        200,
        json={"schema_version": 1, "boards": ["a"], "active_board": None,
              "default_board_path": "/tmp/x"},
    )

    def _transport(request):
        return fake

    with patch("httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "schema_version": 1, "boards": ["a"],
            "active_board": None, "default_board_path": "/tmp/x",
        }
        mock_get.return_value = mock_resp
        client = RemoteKanbanClient(url="http://remote:9119", token="t")
        out = client.health()
        assert out["boards"] == ["a"]


def test_client_raises_on_401():
    with patch("httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "unauthorized"
        mock_get.return_value = mock_resp
        client = RemoteKanbanClient(url="http://remote:9119", token="bad")
        with pytest.raises(RemoteKanbanError, match="401"):
            client.board()


def test_client_raises_on_404():
    with patch("httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "no board"
        mock_get.return_value = mock_resp
        client = RemoteKanbanClient(url="http://remote:9119")
        with pytest.raises(RemoteKanbanError, match="404"):
            client.board(slug="nope")


def test_client_raises_on_network_error():
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        client = RemoteKanbanClient(url="http://remote:9119")
        with pytest.raises(RemoteKanbanError, match="failed"):
            client.health()


def test_client_rejects_older_schema():
    with patch("httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"schema_version": 0, "boards": []}
        mock_get.return_value = mock_resp
        client = RemoteKanbanClient(url="http://remote:9119")
        with pytest.raises(RemoteKanbanError, match="schema_version"):
            client.health()


def test_client_accepts_newer_schema():
    """Forward-compat: future schema version 2 should still work for v1 client."""
    with patch("httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "schema_version": 999, "boards": ["x"],
            "active_board": "x", "default_board_path": "/tmp/x",
        }
        mock_get.return_value = mock_resp
        client = RemoteKanbanClient(url="http://remote:9119")
        out = client.health()
        assert out["boards"] == ["x"]
