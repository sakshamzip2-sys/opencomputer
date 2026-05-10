"""Tests for SPA serving — /  +/{path:spa-fallback}.

When the Vite-built `static/spa/` artifact is present, FastAPI:
- serves `index.html` at `/` (with `__SESSION_TOKEN__` substituted)
- mounts `/assets/*` for hashed JS/CSS
- catches unknown paths (`/sessions`, `/logs`) and serves the SPA so
  React Router resolves them client-side
- explicitly 404s unknown `/api/*`, `/static/*`, `/assets/*`,
  `/fonts/*`, `/ds-assets/*` so the SPA only catches genuine
  navigations
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from opencomputer.dashboard.server import build_app


def _make_static_with_spa(tmp_path: Path) -> Path:
    static = tmp_path / "static"
    spa = static / "spa"
    spa.mkdir(parents=True)
    (spa / "index.html").write_text(
        '<html>'
        '<meta name="oc-session-token" content="__SESSION_TOKEN__" />'
        '<meta name="oc-wire-url" content="__WIRE_URL__" />'
        '<body>SPA-shell</body></html>',
        encoding="utf-8",
    )
    (spa / "assets").mkdir()
    (spa / "assets" / "index-abc.js").write_text("// hashed asset", encoding="utf-8")
    return static


def test_index_serves_spa_when_built(tmp_path: Path):
    static = _make_static_with_spa(tmp_path)
    app = build_app(static_dir=static, enable_pty=False, wire_url="ws://127.0.0.1:18789")
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    # Token + wire URL must be substituted, not literal placeholders
    assert "__SESSION_TOKEN__" not in resp.text
    assert "__WIRE_URL__" not in resp.text
    assert "ws://127.0.0.1:18789" in resp.text
    assert "SPA-shell" in resp.text


def test_spa_fallback_for_unknown_path(tmp_path: Path):
    static = _make_static_with_spa(tmp_path)
    app = build_app(static_dir=static, enable_pty=False)
    resp = TestClient(app).get("/sessions")
    assert resp.status_code == 200
    assert "SPA-shell" in resp.text


def test_api_unknown_returns_404(tmp_path: Path):
    static = _make_static_with_spa(tmp_path)
    app = build_app(static_dir=static, enable_pty=False)
    # Unknown /api/v1/* must NOT fall through to SPA — fail loudly
    resp = TestClient(app).get("/api/v1/does-not-exist")
    assert resp.status_code == 404


def test_assets_unknown_returns_404(tmp_path: Path):
    static = _make_static_with_spa(tmp_path)
    app = build_app(static_dir=static, enable_pty=False)
    resp = TestClient(app).get("/assets/missing-file.js")
    assert resp.status_code == 404


def test_assets_existing_serves(tmp_path: Path):
    static = _make_static_with_spa(tmp_path)
    app = build_app(static_dir=static, enable_pty=False)
    resp = TestClient(app).get("/assets/index-abc.js")
    assert resp.status_code == 200
    assert "hashed asset" in resp.text


def test_no_spa_falls_back_to_legacy_index(tmp_path: Path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>legacy</html>", encoding="utf-8")
    # No `spa/` subdir — legacy index should still serve
    app = build_app(static_dir=static, enable_pty=False)
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert "legacy" in resp.text
