"""Tests for the Hermes A1 dashboard polish endpoints + asset wiring.

Covers:
- ``/static/llm-calls.html`` is served and includes our new scripts.
- ``/api/llm-calls/recent`` returns the right shape (empty + populated).
- ``/api/gateway/restart`` returns a structured response.
- Theme + i18n script files exist and are syntactically reasonable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.dashboard.server import build_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Return a TestClient pointed at a fresh app + temp profile home."""
    profile_home = tmp_path / "home"
    profile_home.mkdir()

    # Redirect default_config().home so the LLM-calls endpoint reads
    # our temp DB instead of the user's real one.
    from opencomputer.agent import config as cfg_mod

    class _C:
        home = profile_home

    monkeypatch.setattr(cfg_mod, "default_config", lambda: _C())

    app = build_app(wire_url="ws://127.0.0.1:0", enable_pty=False)
    from fastapi.testclient import TestClient
    return TestClient(app), profile_home


def _auth_headers() -> dict[str, str]:
    """Probe the session token from the live module to authenticate."""
    from opencomputer.dashboard import server as srv

    return {"Authorization": f"Bearer {srv._SESSION_TOKEN}"}


def test_llm_calls_html_served(client) -> None:
    c, _ = client
    r = c.get("/static/llm-calls.html")
    assert r.status_code == 200
    assert "Recent LLM Calls" in r.text or 'data-i18n="calls.title"' in r.text


def test_llm_calls_html_includes_themes_and_i18n(client) -> None:
    c, _ = client
    r = c.get("/static/llm-calls.html")
    assert r.status_code == 200
    assert "_themes.js" in r.text
    assert "_i18n.js" in r.text


def test_llm_calls_recent_empty(client) -> None:
    c, _ = client
    r = c.get("/api/llm-calls/recent", headers=_auth_headers())
    assert r.status_code == 200
    j = r.json()
    assert j["rows"] == []
    assert j["limit"] == 50


def test_llm_calls_recent_populated(client) -> None:
    c, profile_home = client
    # Initialise SessionDB + insert a row
    from opencomputer.agent.state import SessionDB

    db_path = profile_home / "sessions.db"
    db = SessionDB(db_path)
    db.ensure_session("dash-1", platform="cli")
    db.record_llm_call(
        session_id="dash-1",
        provider="anthropic",
        model="claude-opus-4-7",
        input_tokens=42,
        output_tokens=21,
        cost_usd=0.001,
    )
    r = c.get("/api/llm-calls/recent?limit=10", headers=_auth_headers())
    assert r.status_code == 200
    j = r.json()
    assert j["limit"] == 10
    assert len(j["rows"]) == 1
    row = j["rows"][0]
    assert row["model"] == "claude-opus-4-7"
    assert row["provider"] == "anthropic"
    assert row["input_tokens"] == 42
    assert row["output_tokens"] == 21
    assert row["cost_usd"] == pytest.approx(0.001)


def test_llm_calls_recent_clamps_limit(client) -> None:
    c, _ = client
    r = c.get("/api/llm-calls/recent?limit=99999", headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["limit"] == 500
    r = c.get("/api/llm-calls/recent?limit=0", headers=_auth_headers())
    assert r.status_code == 200
    assert r.json()["limit"] == 50  # 0 falls back to default


def test_gateway_restart_returns_structured_response(client) -> None:
    """The endpoint should return a JSON body with ``ok`` + ``pid`` keys.

    On a live test we only verify the contract, not that the process
    actually re-execs. Real-world callers see ``ok: true`` when the
    signal was delivered.
    """
    c, _ = client
    r = c.post("/api/gateway/restart", headers=_auth_headers())
    assert r.status_code == 200
    j = r.json()
    assert "ok" in j
    # ``pid`` always present (current pid as fallback when no pidfile).
    if j.get("ok"):
        assert "pid" in j
        assert isinstance(j["pid"], int)


# ─── theme + i18n asset existence checks ────────────────────────────────


def _static_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "opencomputer" / "dashboard" / "static"
    )


def test_themes_js_exists_and_defines_themes() -> None:
    js = (_static_dir() / "_themes.js").read_text()
    # Expect the four themes to be defined
    for name in ("dark", "light", "solarized", "monokai"):
        assert f"{name}:" in js or f"'{name}'" in js
    # Public surface
    assert "applyTheme" in js
    assert "renderThemePicker" in js
    assert "OCThemes" in js


def test_i18n_js_exists_and_has_english_keys() -> None:
    js = (_static_dir() / "_i18n.js").read_text()
    # A handful of keys to ensure the LOCALES.en.strings dict is wired
    for key in (
        "tabs.calls",
        "calls.title",
        "calls.empty",
        "mgmt.gateway_restart",
        "header.connecting",
    ):
        assert key in js
    assert "renderLocalePicker" in js
    assert "OCi18n" in js


def test_dashboard_js_renders_calls_tab() -> None:
    js = (_static_dir() / "_dashboard.js").read_text()
    # The new LLM Calls tab must appear in renderNav
    assert "calls" in js
    assert "llm-calls.html" in js


def test_index_html_uses_css_vars() -> None:
    html = (_static_dir() / "index.html").read_text()
    # Spot-check: the original hardcoded #111 background should now be
    # var(--bg, #111). The fallback preserves screenshots.
    assert "var(--bg" in html
    assert "var(--accent" in html
    assert "data-i18n" in html
