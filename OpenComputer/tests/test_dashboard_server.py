"""Tests for opencomputer.dashboard (Wave 6.D — FastAPI migration).

Replaces the original Phase 8.A stdlib http.server tests. The
``DashboardServer`` class still exposes the same public surface
(``start()``/``stop()``/``url``); only the implementation changed.

Mime-type tests were dropped — FastAPI's StaticFiles handles content
types via a maintained library, so the bespoke ``_mime_for`` helper
isn't needed in the new implementation.
"""

from __future__ import annotations

import socket

from opencomputer.dashboard.server import DashboardServer


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_url_property():
    s = DashboardServer(host="127.0.0.1", port=12345)
    assert s.url == "http://127.0.0.1:12345"


def test_app_built_at_construction():
    """``DashboardServer.__init__`` builds the FastAPI app eagerly so
    tests can use TestClient against ``server.app`` without ever
    starting uvicorn."""
    s = DashboardServer(host="127.0.0.1", port=_free_port(), enable_pty=False)
    routes = {r.path for r in s.app.routes if hasattr(r, "path")}
    assert "/api/health" in routes
    assert "/" in routes
    # The plugins-management plugin auto-mounts under /api/plugins/management/
    assert "/api/plugins/management/list" in routes


def test_start_stop_does_not_throw():
    """Smoke: start + stop without sending any traffic. Validates that
    the threaded uvicorn lifecycle doesn't blow up at construction.

    We bind to a random port to avoid collisions on CI.
    """
    s = DashboardServer(host="127.0.0.1", port=_free_port(), enable_pty=False)
    s.start()
    try:
        # Wait briefly for the thread to settle. We don't make HTTP
        # requests here — the route-mounting check above already covers
        # the wire surface, and starting uvicorn in a thread can be
        # slow on CI.
        pass
    finally:
        s.stop(timeout=2.0)


def test_pty_can_be_disabled():
    """Operators that don't need /api/pty (e.g. running on a Windows
    host without WSL) can disable it cleanly."""
    s = DashboardServer(host="127.0.0.1", port=_free_port(), enable_pty=False)
    routes = {r.path for r in s.app.routes if hasattr(r, "path")}
    assert "/api/pty" not in routes
