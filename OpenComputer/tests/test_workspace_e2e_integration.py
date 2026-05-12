"""End-to-end integration test for ``oc workspace`` — runs against a real
in-process dashboard, exercises every mutation endpoint with a real
Bearer token, and asserts SessionDB rows actually changed.

Gated behind ``pytest -m integration`` (per pyproject pytest config) so
this doesn't add 30s to the default suite. Run it with::

    pytest -m integration tests/test_workspace_e2e_integration.py -v

This closes the "mutations only mock-tested" gap from the 2026-05-12
brutal-honest audit. Unit tests with patched DBs prove the route handlers
parse + dispatch correctly; this test proves the wire is correct from
HTTP edge to SQLite row.

What it covers (each is a separate assertion):
* GET  /health, /v1/health, /v1/models, /api/status, /api/sessions,
       /api/skills, /api/skills/categories, /api/jobs, /api/mcp,
       /api/memory — all 200 without auth (per workspace probe rules).
* POST /api/sessions (create) — row lands in SessionDB.
* GET  /api/sessions/{id} — round-trips the created session.
* PATCH /api/sessions/{id} (rename) — title persists to DB.
* POST /api/sessions/{id}/fork — new session + messages cloned.
* DELETE /api/sessions/{id} — row gone from DB.
* GET  /api/skills/{name} 404 for unknown.
* POST /api/sessions/__probe__/chat/stream (no body) — 400, NOT
       404/403/405 (this is the workspace's enhancedChat probe).
* PATCH /api/config — empty body rejected (400).

Does NOT cover (acknowledged scope limit):
* Real LLM streaming — would burn provider tokens; the unit test
  ``test_chat_stream_emits_hermes_sse`` covers the SSE format via a
  mocked AgentLoop.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from opencomputer.dashboard.server import DashboardServer


@pytest.fixture
def live_dashboard(tmp_path: Path) -> Any:
    """Spin up a real DashboardServer on a free port and tear down after.

    Each test gets a fresh server (and therefore a fresh ephemeral
    session token) so they don't leak state across each other.
    """
    import socket

    # Find a free port — bind, get sockname, close, then hand to uvicorn.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = DashboardServer(host="127.0.0.1", port=port)
    server.start()
    # Wait for the server to bind + be ready (max 5s).
    deadline = time.monotonic() + 5.0
    base = f"http://127.0.0.1:{port}"
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.5) as c:
                r = c.get(f"{base}/api/health")
            if r.status_code == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    else:
        server.stop()
        pytest.fail("dashboard did not become ready within 5s")

    token = getattr(server.app.state, "session_token", None)
    assert isinstance(token, str) and len(token) > 16, (
        "dashboard session_token must be a non-empty string"
    )

    try:
        yield {"base": base, "token": token, "server": server}
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Probe surface: every endpoint the workspace's gateway-capabilities probe
# expects must return non-404/403/405 (per gateway-capabilities.ts:425-436)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_every_public_probe_endpoint_returns_200(live_dashboard: dict[str, Any]) -> None:
    base = live_dashboard["base"]
    with httpx.Client(timeout=3.0) as c:
        for path in [
            "/health",
            "/v1/health",
            "/v1/models",
            "/api/status",
            "/api/sessions",
            "/api/skills",
            "/api/skills/categories",
            "/api/jobs",
            "/api/mcp",
            "/api/memory",
        ]:
            r = c.get(f"{base}{path}")
            assert r.status_code == 200, (
                f"{path} returned {r.status_code} — probe would mark missing"
            )


@pytest.mark.integration
def test_api_status_carries_version_for_dashboard_probe(
    live_dashboard: dict[str, Any],
) -> None:
    """probeDashboard() requires ``body.version`` to flip dashboard capability."""
    with httpx.Client(timeout=3.0) as c:
        r = c.get(f"{live_dashboard['base']}/api/status")
    body = r.json()
    assert isinstance(body.get("version"), str) and body["version"]
    assert body.get("status") == "ok"


@pytest.mark.integration
def test_api_config_contains_mcp_servers_key(live_dashboard: dict[str, Any]) -> None:
    """probeMcpConfigKey() requires `mcp_servers` to be present."""
    headers = {"Authorization": f"Bearer {live_dashboard['token']}"}
    with httpx.Client(timeout=3.0) as c:
        r = c.get(f"{live_dashboard['base']}/api/config", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "mcp_servers" in body, (
        "missing `mcp_servers` would prevent mcpFallback from flipping"
    )
    assert isinstance(body["mcp_servers"], list)


# ---------------------------------------------------------------------------
# Mutations: each writes to the real SessionDB; we assert by reading the
# DB file directly (via the live server's profile home).
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_then_get_session_round_trips(live_dashboard: dict[str, Any]) -> None:
    base = live_dashboard["base"]
    headers = {"Authorization": f"Bearer {live_dashboard['token']}"}
    with httpx.Client(timeout=5.0) as c:
        # Create
        r = c.post(
            f"{base}/api/sessions",
            headers=headers,
            json={"title": "E2E created"},
        )
        assert r.status_code == 200, r.text
        sid = r.json()["session"]["id"]
        assert isinstance(sid, str) and len(sid) > 8

        # Read back
        r2 = c.get(f"{base}/api/sessions/{sid}", headers=headers)
        assert r2.status_code == 200
        assert r2.json()["session"]["id"] == sid
        assert r2.json()["session"]["title"] == "E2E created"


@pytest.mark.integration
def test_create_409_on_explicit_id_collision(live_dashboard: dict[str, Any]) -> None:
    import uuid as _uuid

    # Generate a fresh id per test run so the live SessionDB (which is
    # persistent per-profile) doesn't carry state across runs.
    sid = f"e2e-collision-{_uuid.uuid4().hex[:12]}"
    base = live_dashboard["base"]
    headers = {"Authorization": f"Bearer {live_dashboard['token']}"}
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.post(
                f"{base}/api/sessions",
                headers=headers,
                json={"id": sid, "title": "first"},
            )
            assert r.status_code == 200, r.text
            r2 = c.post(
                f"{base}/api/sessions",
                headers=headers,
                json={"id": sid, "title": "second"},
            )
            assert r2.status_code == 409
    finally:
        # Tidy: remove the row so the profile DB stays clean.
        with httpx.Client(timeout=3.0) as c:
            c.delete(f"{base}/api/sessions/{sid}", headers=headers)


@pytest.mark.integration
def test_patch_rename_persists_title_to_db(live_dashboard: dict[str, Any]) -> None:
    base = live_dashboard["base"]
    headers = {"Authorization": f"Bearer {live_dashboard['token']}"}
    with httpx.Client(timeout=5.0) as c:
        r = c.post(
            f"{base}/api/sessions",
            headers=headers,
            json={"title": "old name"},
        )
        sid = r.json()["session"]["id"]

        r2 = c.patch(
            f"{base}/api/sessions/{sid}",
            headers=headers,
            json={"title": "renamed via PATCH"},
        )
        assert r2.status_code == 200
        assert r2.json()["session"]["title"] == "renamed via PATCH"

        # Read back fresh to confirm persistence (not just response echo)
        r3 = c.get(f"{base}/api/sessions/{sid}", headers=headers)
        assert r3.json()["session"]["title"] == "renamed via PATCH"


@pytest.mark.integration
def test_delete_removes_row(live_dashboard: dict[str, Any]) -> None:
    base = live_dashboard["base"]
    headers = {"Authorization": f"Bearer {live_dashboard['token']}"}
    with httpx.Client(timeout=5.0) as c:
        r = c.post(
            f"{base}/api/sessions",
            headers=headers,
            json={"title": "to delete"},
        )
        sid = r.json()["session"]["id"]

        rdel = c.delete(f"{base}/api/sessions/{sid}", headers=headers)
        assert rdel.status_code == 200
        # Workspace's dashboard-shape ``deleteSession`` expects
        # ``{ok: true}`` in the body — a 204 empty response would
        # break its ``dashboardJson()`` parse.
        assert rdel.json()["ok"] is True

        # After delete, GET must 404
        rget = c.get(f"{base}/api/sessions/{sid}", headers=headers)
        assert rget.status_code == 404


@pytest.mark.integration
def test_fork_clones_session_and_messages(live_dashboard: dict[str, Any]) -> None:
    base = live_dashboard["base"]
    headers = {"Authorization": f"Bearer {live_dashboard['token']}"}
    # Set up: create a session and seed two messages directly via SessionDB
    with httpx.Client(timeout=5.0) as c:
        r = c.post(
            f"{base}/api/sessions",
            headers=headers,
            json={"title": "fork-source"},
        )
        sid = r.json()["session"]["id"]

    # Inject two messages into the SessionDB so the fork has something to clone.
    from opencomputer.agent.config import default_config

    cfg = default_config()
    db_path = Path(cfg.session.db_path)
    assert db_path.is_file(), f"expected DB at {db_path}"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'user', 'hello', ?)",
            (sid, time.time()),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'assistant', 'world', ?)",
            (sid, time.time()),
        )
        conn.commit()

    # Fork
    with httpx.Client(timeout=5.0) as c:
        r2 = c.post(f"{base}/api/sessions/{sid}/fork", headers=headers)
    assert r2.status_code == 200, r2.text
    new_sid = r2.json()["session"]["id"]
    assert new_sid != sid

    # Verify the new session has BOTH messages cloned, in order.
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT role, content FROM messages "
            "WHERE session_id = ? ORDER BY id",
            (new_sid,),
        ).fetchall()
    assert [dict(r) for r in rows] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]


# ---------------------------------------------------------------------------
# Probe-edge cases — these MUST NOT 404/403/405 or workspace will downgrade
# the capability.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_chat_stream_probe_with_empty_body_returns_400_not_4045(
    live_dashboard: dict[str, Any],
) -> None:
    """probeEnhancedChatStream() POSTs body `{}`; expects status NOT in
    {404, 403, 405} to mark `enhancedChat` available."""
    base = live_dashboard["base"]
    headers = {
        "Authorization": f"Bearer {live_dashboard['token']}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=5.0) as c:
        r = c.post(
            f"{base}/api/sessions/__probe__/chat/stream",
            headers=headers,
            content=b"{}",
        )
    assert r.status_code == 400, (
        f"expected 400 (probe-pass), got {r.status_code}"
    )
    assert r.status_code not in (403, 404, 405)


@pytest.mark.integration
def test_skill_detail_404_for_unknown(live_dashboard: dict[str, Any]) -> None:
    base = live_dashboard["base"]
    with httpx.Client(timeout=3.0) as c:
        r = c.get(f"{base}/api/skills/definitely-does-not-exist-12345")
    assert r.status_code == 404


@pytest.mark.integration
def test_patch_config_rejects_empty_body(live_dashboard: dict[str, Any]) -> None:
    base = live_dashboard["base"]
    headers = {"Authorization": f"Bearer {live_dashboard['token']}"}
    with httpx.Client(timeout=3.0) as c:
        r = c.patch(f"{base}/api/config", headers=headers, json={})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Auth gating — actual happy + sad paths.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_auth_gated_endpoints_reject_without_bearer(
    live_dashboard: dict[str, Any],
) -> None:
    base = live_dashboard["base"]
    paths = [
        ("GET", "/api/config"),
        ("POST", "/api/sessions"),
        ("PATCH", "/api/sessions/x"),
        ("DELETE", "/api/sessions/x"),
        ("POST", "/api/sessions/x/fork"),
        ("PATCH", "/api/config"),
    ]
    with httpx.Client(timeout=3.0) as c:
        for method, path in paths:
            r = c.request(
                method,
                f"{base}{path}",
                json={"title": "x"} if method in ("POST", "PATCH") else None,
            )
            assert r.status_code == 401, f"{method} {path} returned {r.status_code}"


@pytest.mark.integration
def test_auth_gated_endpoints_reject_wrong_bearer(
    live_dashboard: dict[str, Any],
) -> None:
    base = live_dashboard["base"]
    headers = {"Authorization": "Bearer not-the-right-token-at-all"}
    with httpx.Client(timeout=3.0) as c:
        r = c.get(f"{base}/api/config", headers=headers)
    assert r.status_code == 401
