"""Tests for opencomputer.dashboard (Phase 8.A)."""

from __future__ import annotations

import socket
import time
import urllib.request

import pytest

from opencomputer.dashboard.server import DashboardServer, _mime_for


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server():
    port = _free_port()
    s = DashboardServer(host="127.0.0.1", port=port)
    s.start()
    # Wait for the socket to be ready
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            urllib.request.urlopen(s.url + "/", timeout=0.5).read()
            break
        except Exception:
            time.sleep(0.05)
    yield s
    s.stop()


# ---------- Static serving ----------


def test_index_served(server):
    r = urllib.request.urlopen(server.url + "/")
    assert r.status == 200
    body = r.read().decode("utf-8")
    assert "OpenComputer" in body
    assert "<script>" in body  # SPA bootstrap is inline


def test_index_injects_wire_url(server):
    """The placeholder __WIRE_URL__ must be replaced with the real URL."""
    r = urllib.request.urlopen(server.url + "/")
    body = r.read().decode("utf-8")
    assert server.wire_url in body
    assert "__WIRE_URL__" not in body


def test_unknown_path_404(server):
    try:
        urllib.request.urlopen(server.url + "/nonexistent")
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        pytest.fail("expected 404")


def test_static_path_traversal_refused(server):
    """A ../ in the static path must NOT escape the static dir."""
    try:
        urllib.request.urlopen(server.url + "/static/../server.py")
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        pytest.fail("expected 404 for path traversal")


# ---------- CSP / security headers ----------


def test_security_headers_present(server):
    r = urllib.request.urlopen(server.url + "/")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "ws://127.0.0.1" in csp


# ---------- Mime type helper ----------


@pytest.mark.parametrize("name,mime", [
    ("style.css", "text/css; charset=utf-8"),
    ("app.js", "application/javascript; charset=utf-8"),
    ("page.html", "text/html; charset=utf-8"),
    ("icon.svg", "image/svg+xml"),
    ("img.png", "image/png"),
    ("anything.bin", "application/octet-stream"),
])
def test_mime_for_picks_correct_type(name, mime):
    assert _mime_for(name) == mime


# ---------- Server lifecycle ----------


def test_start_stop_idempotent(tmp_path):
    s = DashboardServer(host="127.0.0.1", port=_free_port())
    s.start()
    s.start()  # second start is a no-op (no double-bind)
    s.stop()
    s.stop()  # second stop is a no-op


def test_url_property():
    s = DashboardServer(host="127.0.0.1", port=12345)
    assert s.url == "http://127.0.0.1:12345"
