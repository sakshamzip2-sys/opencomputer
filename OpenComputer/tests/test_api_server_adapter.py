"""Tests for the API Server channel adapter (G.28 / Tier 4.x).

REST endpoint exposing the agent. Uses aiohttp's test client (no real
ports bound) for the HTTP-handler tests, and a ``net.aio_server``
fixture only where lifecycle (connect/disconnect) is being tested.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from plugin_sdk import ChannelCapabilities


def _load():
    spec = importlib.util.spec_from_file_location(
        "api_server_adapter_test_g28",
        Path(__file__).resolve().parent.parent / "extensions" / "api-server" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.APIServerAdapter, mod


@pytest.fixture
async def adapter_with_handler():
    APIServerAdapter, _ = _load()
    a = APIServerAdapter(
        config={"host": "127.0.0.1", "port": 0, "token": "secret-token"}
    )
    captured: list[tuple[str, str]] = []

    async def handler(session_id: str, message: str) -> str:
        captured.append((session_id, message))
        return f"echo: {message}"

    a.set_handler(handler)
    return a, captured


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_no_message_capabilities(self) -> None:
        APIServerAdapter, _ = _load()
        # Request/response surface — none of the chat-shape flags apply.
        assert APIServerAdapter.capabilities == ChannelCapabilities(0)


# ---------------------------------------------------------------------------
# Endpoint shape
# ---------------------------------------------------------------------------


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_authorized_chat_returns_handler_reply(
        self, adapter_with_handler
    ) -> None:
        adapter, captured = adapter_with_handler
        app = adapter._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/chat",
                headers={"Authorization": "Bearer secret-token"},
                json={"session_id": "s1", "message": "hello"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["session_id"] == "s1"
            assert data["response"] == "echo: hello"
        assert captured == [("s1", "hello")]

    @pytest.mark.asyncio
    async def test_missing_auth_header_rejected(
        self, adapter_with_handler
    ) -> None:
        adapter, _ = adapter_with_handler
        app = adapter._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/chat", json={"message": "hi"}
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(
        self, adapter_with_handler
    ) -> None:
        adapter, _ = adapter_with_handler
        app = adapter._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/chat",
                headers={"Authorization": "Bearer wrong-token"},
                json={"message": "hi"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(
        self, adapter_with_handler
    ) -> None:
        adapter, _ = adapter_with_handler
        app = adapter._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/chat",
                headers={
                    "Authorization": "Bearer secret-token",
                    "Content-Type": "application/json",
                },
                data="{not valid json",
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_empty_message_rejected(
        self, adapter_with_handler
    ) -> None:
        adapter, _ = adapter_with_handler
        app = adapter._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/chat",
                headers={"Authorization": "Bearer secret-token"},
                json={"message": "  "},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_no_handler_set_returns_503(self) -> None:
        APIServerAdapter, _ = _load()
        a = APIServerAdapter(
            config={"host": "127.0.0.1", "port": 0, "token": "tok"}
        )
        # Note: NOT calling set_handler.
        app = a._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/chat",
                headers={"Authorization": "Bearer tok"},
                json={"message": "hi"},
            )
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_handler_exception_returns_500(self) -> None:
        APIServerAdapter, _ = _load()
        a = APIServerAdapter(
            config={"host": "127.0.0.1", "port": 0, "token": "tok"}
        )

        async def boom(_session_id: str, _message: str) -> str:
            raise RuntimeError("kaboom")

        a.set_handler(boom)
        app = a._build_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/v1/chat",
                headers={"Authorization": "Bearer tok"},
                json={"message": "hi"},
            )
            assert resp.status == 500
            data = await resp.json()
            assert "RuntimeError" in data["error"]


# ---------------------------------------------------------------------------
# send() — shouldn't be a push channel
# ---------------------------------------------------------------------------


class TestSendNotApplicable:
    @pytest.mark.asyncio
    async def test_send_returns_clear_error(self) -> None:
        APIServerAdapter, _ = _load()
        a = APIServerAdapter(
            config={"host": "127.0.0.1", "port": 0, "token": "tok"}
        )
        result = await a.send("anywhere", "hello")
        assert not result.success
        assert "REST endpoint" in result.error or "request" in result.error.lower()
