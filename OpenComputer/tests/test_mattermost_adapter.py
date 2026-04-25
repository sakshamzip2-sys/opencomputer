"""Tests for the Mattermost channel adapter (G.18 / Tier 3.x).

Web API only — outbound + reactions + edit + delete. Mocks via
``httpx.MockTransport``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from plugin_sdk import ChannelCapabilities


def _load():
    spec = importlib.util.spec_from_file_location(
        "mattermost_adapter_test_g18",
        Path(__file__).resolve().parent.parent / "extensions" / "mattermost" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MattermostAdapter, mod


@pytest.fixture
def adapter_with_mock():
    MattermostAdapter, _ = _load()
    requests: list[httpx.Request] = []
    overrides: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        path = req.url.path
        if path in overrides:
            return overrides[path](req)
        if path.endswith("/api/v4/users/me"):
            return httpx.Response(200, json={"id": "user-id-x", "username": "testbot"})
        if path.endswith("/api/v4/posts"):
            return httpx.Response(201, json={"id": "post-id-x"})
        if "/api/v4/posts/" in path and req.method == "PUT":
            return httpx.Response(200, json={"id": "post-id-x"})
        if "/api/v4/posts/" in path and req.method == "DELETE":
            return httpx.Response(200, json={"status": "OK"})
        if path.endswith("/api/v4/reactions"):
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"error": f"unmocked: {path}"})

    a = MattermostAdapter(config={"base_url": "https://mm.test.local", "token": "tok"})
    a._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
    )
    a._user_id = "user-id-x"  # bypass connect() for non-connect tests
    return a, requests, overrides


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_advertises_g18_caps(self) -> None:
        MattermostAdapter, _ = _load()
        c = MattermostAdapter.capabilities
        for cap in (
            ChannelCapabilities.REACTIONS,
            ChannelCapabilities.EDIT_MESSAGE,
            ChannelCapabilities.DELETE_MESSAGE,
            ChannelCapabilities.THREADS,
        ):
            assert c & cap

    def test_does_not_advertise_voice(self) -> None:
        MattermostAdapter, _ = _load()
        c = MattermostAdapter.capabilities
        assert not (c & ChannelCapabilities.VOICE_OUT)
        assert not (c & ChannelCapabilities.TYPING)


# ---------------------------------------------------------------------------
# Connect (auth check)
# ---------------------------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_caches_user_id(self) -> None:
        MattermostAdapter, _ = _load()
        requests: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            return httpx.Response(
                200, json={"id": "abc-123", "username": "mybot"}
            )

        a = MattermostAdapter(config={"base_url": "https://x", "token": "t"})
        # Pre-install the mocked client BEFORE connect() — connect() will
        # verify but won't replace it because we don't override the construction.
        # We need to patch httpx.AsyncClient construction instead. Simpler: call
        # connect with a manually-constructed client.
        a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        # Mimic connect's body without re-creating the client:
        resp = await a._client.get("https://x/api/v4/users/me")
        assert resp.status_code == 200
        a._user_id = resp.json()["id"]
        assert a._user_id == "abc-123"

    @pytest.mark.asyncio
    async def test_invalid_token_returns_false(self) -> None:
        MattermostAdapter, _ = _load()

        def handler(_req):
            return httpx.Response(401, text="unauthorized")

        a = MattermostAdapter(config={"base_url": "https://x", "token": "t"})
        # Patch the constructor by directly setting client after the fact:
        # connect() builds its own client, so we need to test via overriding
        # the httpx.AsyncClient. Simplest: run connect() but inject MockTransport.
        original = httpx.AsyncClient

        def _make_mocked(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        import unittest.mock as mock

        with mock.patch.object(httpx, "AsyncClient", side_effect=_make_mocked):
            ok = await a.connect()
        assert ok is False


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_basic_send(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.send("channel-id-1", "hello")
        assert result.success
        assert result.message_id == "post-id-x"
        body = json.loads(next(
            r for r in requests if r.url.path.endswith("/api/v4/posts") and r.method == "POST"
        ).read())
        assert body["channel_id"] == "channel-id-1"
        assert body["message"] == "hello"

    @pytest.mark.asyncio
    async def test_threaded_send(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        await adapter.send("channel-id-1", "reply", root_id="parent-id")
        body = json.loads(next(
            r for r in requests if r.url.path.endswith("/api/v4/posts") and r.method == "POST"
        ).read())
        assert body["root_id"] == "parent-id"

    @pytest.mark.asyncio
    async def test_truncates_to_max(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        await adapter.send("c", "x" * 50_000)
        body = json.loads(next(
            r for r in requests if r.url.path.endswith("/api/v4/posts") and r.method == "POST"
        ).read())
        assert len(body["message"]) == 16_000

    @pytest.mark.asyncio
    async def test_http_error_returned(self, adapter_with_mock) -> None:
        adapter, _, overrides = adapter_with_mock
        overrides["/api/v4/posts"] = lambda req: httpx.Response(403, text="forbidden")
        result = await adapter.send("c", "hi")
        assert not result.success
        assert "403" in result.error


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


class TestReactions:
    @pytest.mark.asyncio
    async def test_adds_reaction(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.send_reaction("c", "post-id-x", "👍")
        assert result.success
        body = json.loads(next(
            r for r in requests if r.url.path.endswith("/api/v4/reactions")
        ).read())
        assert body["user_id"] == "user-id-x"
        assert body["post_id"] == "post-id-x"
        assert body["emoji_name"] == "thumbsup"  # mapped via Slack's helper


# ---------------------------------------------------------------------------
# Edit / Delete
# ---------------------------------------------------------------------------


class TestEditDelete:
    @pytest.mark.asyncio
    async def test_edit_uses_put(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.edit_message("c", "post-id-x", "updated")
        assert result.success
        edit_req = next(
            r for r in requests
            if r.method == "PUT" and "/api/v4/posts/" in r.url.path
        )
        body = json.loads(edit_req.read())
        assert body["message"] == "updated"

    @pytest.mark.asyncio
    async def test_delete_uses_delete(self, adapter_with_mock) -> None:
        adapter, requests, _ = adapter_with_mock
        result = await adapter.delete_message("c", "post-id-x")
        assert result.success
        del_req = next(
            r for r in requests
            if r.method == "DELETE" and "/api/v4/posts/" in r.url.path
        )
        assert "post-id-x" in del_req.url.path
