"""Tests for browser-control client/fetch.py — dual transport fork,
status mapping, no retries, rate-limit static hint, no upstream
reflection."""

from __future__ import annotations

from typing import Any

import pytest
from extensions.browser_control._utils.errors import BrowserServiceError
from extensions.browser_control.client.fetch import (
    fetch_browser_json,
    set_default_dispatcher_app,
)


class _FakeDispatchResult:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}


class _DispatchAppStub:
    """Minimal "app" that the patched dispatcher closure inspects."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []


@pytest.fixture
def dispatcher_recorder(monkeypatch):
    """Patch ``dispatch_browser_control_request`` to return a programmable
    response and record the call args."""
    captured: dict[str, Any] = {}
    response: dict[str, Any] = {"status": 200, "body": {"ok": True}}

    async def _fake_dispatch(app, **kwargs):
        captured.update(kwargs)
        captured["app"] = app
        return _FakeDispatchResult(
            status=response["status"],
            body=response["body"],
        )

    monkeypatch.setattr(
        "extensions.browser_control.client.fetch."
        "dispatch_browser_control_request",
        _fake_dispatch,
    )

    def setter(*, status: int = 200, body: Any = None):
        response["status"] = status
        response["body"] = body if body is not None else {"ok": True}

    return captured, setter


class TestDispatcherTransport:
    @pytest.mark.asyncio
    async def test_path_only_routes_through_dispatcher(self, dispatcher_recorder):
        captured, _ = dispatcher_recorder
        app = _DispatchAppStub()
        result = await fetch_browser_json(
            "GET", "/snapshot", dispatcher_app=app, timeout=5.0
        )
        assert result == {"ok": True}
        assert captured["method"] == "GET"
        assert captured["path"] == "/snapshot"
        assert captured["app"] is app

    @pytest.mark.asyncio
    async def test_dispatcher_4xx_surfaces_message(self, dispatcher_recorder):
        captured, set_response = dispatcher_recorder
        set_response(status=400, body={"error": {"message": "bad arg"}})
        app = _DispatchAppStub()
        with pytest.raises(BrowserServiceError) as exc_info:
            await fetch_browser_json("GET", "/snapshot", dispatcher_app=app)
        assert "bad arg" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_dispatcher_429_uses_static_hint(self, dispatcher_recorder):
        captured, set_response = dispatcher_recorder
        # Server returns a body we MUST NOT reflect
        set_response(status=429, body={"error": {"message": "evil <script>"}})
        app = _DispatchAppStub()
        with pytest.raises(BrowserServiceError) as exc_info:
            await fetch_browser_json("GET", "/snapshot", dispatcher_app=app)
        msg = str(exc_info.value).lower()
        assert "rate limit" in msg
        assert "evil" not in msg, "must not reflect upstream body for 429"

    @pytest.mark.asyncio
    async def test_no_dispatcher_app_raises(self, monkeypatch):
        # Reset the module-level default
        set_default_dispatcher_app(None)
        with pytest.raises(BrowserServiceError) as exc_info:
            await fetch_browser_json("GET", "/snapshot", timeout=1.0)
        assert "dispatcher" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_query_string_split(self, dispatcher_recorder):
        captured, _ = dispatcher_recorder
        app = _DispatchAppStub()
        await fetch_browser_json(
            "GET", "/console?level=error&targetId=t1", dispatcher_app=app
        )
        assert captured["path"] == "/console"
        assert captured["query"]["level"] == ["error"]
        assert captured["query"]["targetId"] == ["t1"]


class TestHttpTransport:
    @pytest.mark.asyncio
    async def test_non_loopback_refused(self):
        with pytest.raises(BrowserServiceError) as exc_info:
            await fetch_browser_json(
                "GET", "https://evil.com/snapshot", timeout=1.0
            )
        assert "non-loopback" in str(exc_info.value).lower()


class TestNoRetry:
    """The TS source explicitly disabled retries on 4xx/5xx — same here."""

    @pytest.mark.asyncio
    async def test_4xx_no_retry(self, dispatcher_recorder):
        captured, set_response = dispatcher_recorder
        call_count = 0

        async def counting_dispatch(app, **kwargs):
            nonlocal call_count
            call_count += 1
            return _FakeDispatchResult(status=401, body={"error": {"message": "nope"}})

        # patch directly
        from extensions.browser_control.client import fetch as fetch_mod
        fetch_mod.dispatch_browser_control_request = counting_dispatch

        with pytest.raises(BrowserServiceError):
            await fetch_browser_json(
                "GET", "/snapshot", dispatcher_app=_DispatchAppStub()
            )
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_5xx_no_retry(self, dispatcher_recorder):
        captured, set_response = dispatcher_recorder
        call_count = 0

        async def counting_dispatch(app, **kwargs):
            nonlocal call_count
            call_count += 1
            return _FakeDispatchResult(status=500, body={"error": {"message": "boom"}})

        from extensions.browser_control.client import fetch as fetch_mod
        fetch_mod.dispatch_browser_control_request = counting_dispatch

        with pytest.raises(BrowserServiceError):
            await fetch_browser_json(
                "GET", "/snapshot", dispatcher_app=_DispatchAppStub()
            )
        assert call_count == 1
