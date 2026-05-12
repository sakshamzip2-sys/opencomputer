"""Tests for opencomputer.dashboard.routes.hermes_aliases.

These verify the /api/* alias surface returns 200s in the
hermes-agent shape and that the gateway-capabilities probe in
hermes-workspace will flip the corresponding ``missing[]`` entries
to ``available``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencomputer.dashboard.routes.hermes_aliases import router


def _build_app(*, with_token: bool = False) -> FastAPI:
    app = FastAPI()
    app.state.session_token = "tok" if with_token else None
    app.include_router(router)
    return app


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer tok"}


# ---------------------------------------------------------------------------
# /api/sessions
# ---------------------------------------------------------------------------


def test_list_sessions_returns_hermes_shape() -> None:
    app = _build_app()
    client = TestClient(app)

    async def _fake(*, limit: int, channel: str | None = None) -> dict[str, Any]:
        return {
            "items": [
                {"id": "s1", "title": "first"},
                {"id": "s2", "title": "second"},
            ],
            "limit": limit,
        }

    with patch(
        "opencomputer.dashboard.routes.sessions.list_sessions",
        new=AsyncMock(side_effect=_fake),
    ):
        resp = client.get("/api/sessions?limit=10&offset=0")

    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and "total" in body and "limit" in body
    assert len(body["items"]) == 2
    assert body["items"][0]["id"] == "s1"


def test_list_sessions_paginates_via_offset() -> None:
    app = _build_app()
    client = TestClient(app)

    async def _fake(*, limit: int, channel: str | None = None) -> dict[str, Any]:
        return {
            "items": [{"id": f"s{i}"} for i in range(5)],
            "limit": limit,
        }

    with patch(
        "opencomputer.dashboard.routes.sessions.list_sessions",
        new=AsyncMock(side_effect=_fake),
    ):
        resp = client.get("/api/sessions?limit=2&offset=2")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["id"] == "s2"
    assert len(body["items"]) == 2


def test_get_session_wraps_in_session_key() -> None:
    app = _build_app()
    client = TestClient(app)

    async def _fake(session_id: str) -> dict[str, Any]:
        assert session_id == "abc"
        return {"id": "abc", "title": "Test"}

    with patch(
        "opencomputer.dashboard.routes.sessions.get_session",
        new=AsyncMock(side_effect=_fake),
    ):
        resp = client.get("/api/sessions/abc")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"session": {"id": "abc", "title": "Test"}}


def test_get_messages_passes_through() -> None:
    app = _build_app()
    client = TestClient(app)

    async def _fake(session_id: str, *, limit: int, offset: int) -> dict[str, Any]:
        return {"items": [{"id": 1, "content": "hi"}], "limit": limit, "offset": offset, "total": 1}

    with patch(
        "opencomputer.dashboard.routes.sessions.get_messages",
        new=AsyncMock(side_effect=_fake),
    ):
        resp = client.get("/api/sessions/abc/messages?limit=5&offset=0")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["content"] == "hi"


def test_search_sessions_returns_hermes_shape() -> None:
    app = _build_app()
    client = TestClient(app)

    async def _fake(*, q: str, limit: int) -> dict[str, Any]:
        return {"items": [{"id": "s9"}], "limit": limit, "query": q}

    with patch(
        "opencomputer.dashboard.routes.sessions.search_sessions",
        new=AsyncMock(side_effect=_fake),
    ):
        resp = client.get("/api/sessions/search?q=foo&limit=10")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"query": "foo", "count": 1, "results": [{"id": "s9"}]}


# ---------------------------------------------------------------------------
# /api/skills
# ---------------------------------------------------------------------------


def test_list_skills_normalizes_oc_payload_with_items() -> None:
    app = _build_app()
    client = TestClient(app)

    async def _fake() -> dict[str, Any]:
        return {"items": [{"name": "skill1"}, {"name": "skill2"}]}

    with patch(
        "opencomputer.dashboard.routes.skills.list_skills",
        new=AsyncMock(side_effect=_fake),
    ):
        resp = client.get("/api/skills")
    assert resp.status_code == 200
    body = resp.json()
    assert "skills" in body
    assert len(body["skills"]) == 2


def test_list_skills_returns_empty_when_oc_fails() -> None:
    app = _build_app()
    client = TestClient(app)

    async def _fail() -> dict[str, Any]:
        raise RuntimeError("registry boom")

    with patch(
        "opencomputer.dashboard.routes.skills.list_skills",
        new=AsyncMock(side_effect=_fail),
    ):
        resp = client.get("/api/skills")
    # 200 with empty list + surfaced error — workspace tolerates empty
    # data; missing endpoint would yield 404 which the probe interprets
    # as offline. The ``error`` key gives an operator looking at the
    # response a clear signal of WHY it's empty.
    assert resp.status_code == 200
    body = resp.json()
    assert body["skills"] == []
    assert "registry boom" in body["error"]


def test_skill_categories_distinct_only() -> None:
    app = _build_app()
    client = TestClient(app)

    async def _fake() -> dict[str, Any]:
        return {
            "items": [
                {"name": "a", "category": "tools"},
                {"name": "b", "category": "tools"},
                {"name": "c", "category": "research"},
                {"name": "d"},  # no category
            ]
        }

    with patch(
        "opencomputer.dashboard.routes.skills.list_skills",
        new=AsyncMock(side_effect=_fake),
    ):
        resp = client.get("/api/skills/categories")
    body = resp.json()
    assert sorted(body["categories"]) == ["research", "tools"]


# ---------------------------------------------------------------------------
# /api/jobs
# ---------------------------------------------------------------------------


def test_list_jobs_returns_hermes_shape() -> None:
    app = _build_app()
    client = TestClient(app)

    async def _fake() -> dict[str, Any]:
        return {"jobs": [{"id": "j1", "name": "daily-roll"}]}

    with patch(
        "opencomputer.dashboard.routes.cron.list_jobs",
        new=AsyncMock(side_effect=_fake),
    ):
        resp = client.get("/api/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"][0]["id"] == "j1"


def test_list_jobs_empty_on_failure() -> None:
    app = _build_app()
    client = TestClient(app)

    async def _fail() -> dict[str, Any]:
        raise RuntimeError("no cron table")

    with patch(
        "opencomputer.dashboard.routes.cron.list_jobs",
        new=AsyncMock(side_effect=_fail),
    ):
        resp = client.get("/api/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"] == []
    assert "no cron table" in body["error"]


# ---------------------------------------------------------------------------
# /api/config
# ---------------------------------------------------------------------------


def test_config_requires_bearer() -> None:
    app = _build_app(with_token=True)
    client = TestClient(app)
    resp = client.get("/api/config")
    assert resp.status_code == 401


def test_config_returns_oc_payload_when_authed(auth_headers: dict[str, str]) -> None:
    app = _build_app(with_token=True)
    client = TestClient(app)

    async def _fake() -> dict[str, Any]:
        return {"model": {"provider": "anthropic", "model": "claude-opus-4-7"}}

    with patch(
        "opencomputer.dashboard.routes.config.get_config",
        new=AsyncMock(side_effect=_fake),
    ):
        resp = client.get("/api/config", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["model"]["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# /api/mcp
# ---------------------------------------------------------------------------


def test_mcp_returns_empty_when_manager_missing() -> None:
    app = _build_app()
    client = TestClient(app)

    # No instance available
    with patch(
        "opencomputer.mcp.client.MCPManager",
        MagicMock(get_instance=MagicMock(return_value=None)),
    ):
        resp = client.get("/api/mcp")
    assert resp.status_code == 200
    assert resp.json() == {"servers": []}


def test_mcp_enumerates_servers_when_available() -> None:
    app = _build_app()
    client = TestClient(app)

    fake_manager = MagicMock()
    fake_manager.list_servers.return_value = [
        {"name": "fs-mcp", "transport": "stdio"},
        {"name": "github", "transport": "http"},
    ]
    with patch(
        "opencomputer.mcp.client.MCPManager",
        MagicMock(get_instance=MagicMock(return_value=fake_manager)),
    ):
        resp = client.get("/api/mcp")

    body = resp.json()
    assert resp.status_code == 200
    names = {s["name"] for s in body["servers"]}
    assert names == {"fs-mcp", "github"}


# ---------------------------------------------------------------------------
# Probe-pass: every aliased endpoint must NOT return 404 or 403 from
# the workspace's gateway-capabilities perspective (those are the two
# status codes that map to "missing").
# ---------------------------------------------------------------------------


def test_every_alias_probe_succeeds_under_capability_rules() -> None:
    """Sanity: the workspace's probe maps status not in {404, 403} to
    "available". This test asserts each route can produce a non-404/403
    response with reasonable inputs (no auth header where not required).
    """
    app = _build_app()
    client = TestClient(app)

    paths = [
        "/api/sessions",
        "/api/skills",
        "/api/skills/categories",
        "/api/jobs",
        "/api/mcp",
    ]
    # Patch the underlying handlers to return empty payloads — the
    # important thing here is route registration + no 404/403.
    with (
        patch(
            "opencomputer.dashboard.routes.sessions.list_sessions",
            new=AsyncMock(return_value={"items": []}),
        ),
        patch(
            "opencomputer.dashboard.routes.skills.list_skills",
            new=AsyncMock(return_value={"items": []}),
        ),
        patch(
            "opencomputer.dashboard.routes.cron.list_jobs",
            new=AsyncMock(return_value={"jobs": []}),
        ),
    ):
        for path in paths:
            resp = client.get(path)
            assert resp.status_code not in (403, 404), (
                f"{path} returned {resp.status_code} — "
                "workspace would mark this endpoint as missing"
            )
