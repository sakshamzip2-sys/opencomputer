"""Tests for PR4 (oauth/cron) + PR5 (config/env) routes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opencomputer.dashboard.server import build_app


def _client() -> TestClient:
    return TestClient(build_app(enable_pty=False))


# ---------- OAuth ----------


def test_oauth_list():
    r = _client().get("/api/v1/providers/oauth")
    assert r.status_code == 200
    assert "items" in r.json()


def test_oauth_start_returns_session_id():
    r = _client().post("/api/v1/providers/oauth/anthropic/start", json={})
    assert r.status_code == 200
    body = r.json()
    assert "session_id" in body
    assert body["status"] == "pending"


def test_oauth_submit_validates_code():
    r = _client().post("/api/v1/providers/oauth/anthropic/submit", json={"code": ""})
    assert r.status_code == 400


def test_oauth_poll_404_when_session_unknown():
    r = _client().get("/api/v1/providers/oauth/anthropic/poll/never-existed")
    assert r.status_code == 404


# ---------- Cron ----------


def test_cron_jobs_list():
    r = _client().get("/api/v1/cron/jobs")
    assert r.status_code == 200
    assert "items" in r.json()


def test_cron_create_validates():
    r = _client().post("/api/v1/cron/jobs", json={})
    assert r.status_code == 422


def test_cron_get_404_when_missing():
    r = _client().get("/api/v1/cron/jobs/does-not-exist")
    assert r.status_code in (404, 503)


# ---------- Config ----------


def test_config_get():
    r = _client().get("/api/v1/config")
    assert r.status_code in (200, 503)


def test_config_defaults():
    r = _client().get("/api/v1/config/defaults")
    assert r.status_code in (200, 503)


def test_config_raw_get():
    r = _client().get("/api/v1/config/raw")
    assert r.status_code == 200
    body = r.json()
    assert "path" in body and "text" in body


# ---------- Env ----------


def test_env_list_redacts_values(tmp_path, monkeypatch):
    """Values must NEVER be returned by /env GET."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("MY_SECRET=super-secret-value\n", encoding="utf-8")
    r = _client().get("/api/v1/env")
    assert r.status_code == 200
    body = r.json()
    text = str(body)
    assert "super-secret-value" not in text
    assert any(k["key"] == "MY_SECRET" for k in body["items"])


def test_env_reveal_requires_confirm_header(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("MY_SECRET=val\n", encoding="utf-8")
    # Without header → 403
    r = _client().post("/api/v1/env/reveal", json={"key": "MY_SECRET", "value": "ignored"})
    assert r.status_code == 403


def test_env_reveal_with_confirm_returns_value(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("REVEALED=val123\n", encoding="utf-8")
    r = _client().post(
        "/api/v1/env/reveal",
        json={"key": "REVEALED", "value": "ignored"},
        headers={"X-OC-Confirm": "yes"},
    )
    assert r.status_code == 200
    assert r.json()["value"] == "val123"


def test_env_put_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = _client().put("/api/v1/env", json={"key": "ABC", "value": "123"})
    assert r.status_code == 200
    text = (tmp_path / ".env").read_text()
    assert "ABC=123" in text


def test_env_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("FOO=bar\n", encoding="utf-8")
    r = _client().delete("/api/v1/env?key=FOO")
    assert r.status_code == 200
    assert r.json()["existed"] is True
    text = (tmp_path / ".env").read_text()
    assert "FOO" not in text
