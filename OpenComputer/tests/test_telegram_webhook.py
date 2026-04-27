"""Telegram webhook mode (Round 4 Item 3).

Pinned to Saksham's original blocker on this work: he can run a
tunnel (ngrok / cloudflared) on Mac OR deploy to a VPS, so the HTTPS
requirement is no longer disqualifying. Polling stays the default;
webhook is opt-in via config.

This file tests the WEBHOOK HELPER in isolation. The adapter
integration test would need a live aiohttp + Telegram simulator;
deferred until a follow-up.
"""
from __future__ import annotations

import pytest

# ─── secret token generation ─────────────────────────────────────────


def test_generate_secret_token_is_telegram_compatible() -> None:
    """Telegram requires 1-256 chars, only ``A-Za-z0-9_-``."""
    from extensions.telegram.webhook_helper import generate_secret_token

    for _ in range(10):
        tok = generate_secret_token()
        assert 1 <= len(tok) <= 256
        assert all(c.isalnum() or c in "_-" for c in tok), (
            f"secret_token {tok!r} contains chars Telegram won't accept"
        )


def test_generate_secret_token_unique_per_call() -> None:
    """Each call produces a different secret (no caching, no determinism)."""
    from extensions.telegram.webhook_helper import generate_secret_token

    tokens = {generate_secret_token() for _ in range(50)}
    assert len(tokens) == 50, "secret_token generator must not collide"


# ─── set / delete / get_info via Telegram API (mocked) ────────────────


@pytest.mark.asyncio
async def test_set_webhook_sends_correct_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """setWebhook posts {url, secret_token, ...} to the Bot API."""
    from extensions.telegram import webhook_helper

    captured: dict = {}

    class FakeResponse:
        @staticmethod
        def json():
            return {"ok": True, "description": "Webhook was set"}

    class FakeClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url: str, json: dict):
            captured["url"] = url
            captured["body"] = json
            return FakeResponse()

    monkeypatch.setattr(webhook_helper.httpx, "AsyncClient", FakeClient)

    ok, msg = await webhook_helper.set_webhook(
        token="abc:def",
        url="https://tunnel.example.com/telegram/webhook",
        secret_token="s3cret",
        drop_pending=True,
        allowed_updates=["message", "callback_query"],
    )

    assert ok is True
    assert "abc:def" in captured["url"]
    assert captured["body"]["url"] == "https://tunnel.example.com/telegram/webhook"
    assert captured["body"]["secret_token"] == "s3cret"
    assert captured["body"]["drop_pending_updates"] is True
    assert captured["body"]["allowed_updates"] == ["message", "callback_query"]


@pytest.mark.asyncio
async def test_set_webhook_returns_error_on_api_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bot API returns ok=False → return False + error description."""
    from extensions.telegram import webhook_helper

    class FakeResponse:
        @staticmethod
        def json():
            return {"ok": False, "description": "Bad webhook URL"}

    class FakeClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_, **__):
            return FakeResponse()

    monkeypatch.setattr(webhook_helper.httpx, "AsyncClient", FakeClient)

    ok, msg = await webhook_helper.set_webhook(
        token="x", url="https://x", secret_token="y"
    )
    assert ok is False
    assert "Bad webhook URL" in msg


@pytest.mark.asyncio
async def test_delete_webhook_handles_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network blip during deleteWebhook is handled, not raised."""
    from extensions.telegram import webhook_helper

    class BoomClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_, **__):
            raise RuntimeError("network down")

    monkeypatch.setattr(webhook_helper.httpx, "AsyncClient", BoomClient)

    ok, msg = await webhook_helper.delete_webhook(token="x")
    assert ok is False
    assert "HTTP failure" in msg


# ─── secret-token verification ────────────────────────────────────────


def test_verify_secret_header_passes_on_match() -> None:
    """Constant-time comparison: matching secret returns True."""
    from extensions.telegram.webhook_helper import _verify_secret_header

    class FakeRequest:
        headers = {"X-Telegram-Bot-Api-Secret-Token": "expected-secret"}

    assert _verify_secret_header(FakeRequest(), "expected-secret") is True


def test_verify_secret_header_fails_on_mismatch() -> None:
    from extensions.telegram.webhook_helper import _verify_secret_header

    class FakeRequest:
        headers = {"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"}

    assert _verify_secret_header(FakeRequest(), "expected-secret") is False


def test_verify_secret_header_fails_on_missing() -> None:
    """No header at all = forged request, drop it."""
    from extensions.telegram.webhook_helper import _verify_secret_header

    class FakeRequest:
        headers: dict = {}

    assert _verify_secret_header(FakeRequest(), "expected-secret") is False


# ─── tunnel detection ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detect_ngrok_url_finds_https_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ngrok's /api/tunnels response → return the https public_url."""
    from extensions.telegram import webhook_helper

    class FakeResponse:
        @staticmethod
        def json():
            return {
                "tunnels": [
                    {"public_url": "http://abc.ngrok.io"},
                    {"public_url": "https://abc.ngrok.io"},
                ]
            }

    class FakeClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url: str):
            assert "127.0.0.1:4040" in url
            return FakeResponse()

    monkeypatch.setattr(webhook_helper.httpx, "AsyncClient", FakeClient)

    url = await webhook_helper.detect_ngrok_url()
    assert url == "https://abc.ngrok.io"


@pytest.mark.asyncio
async def test_detect_ngrok_url_returns_none_when_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection refused on :4040 → return None (don't crash)."""
    from extensions.telegram import webhook_helper

    class BoomClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(webhook_helper.httpx, "AsyncClient", BoomClient)

    assert await webhook_helper.detect_ngrok_url() is None


def test_detect_cloudflared_running_handles_missing_pgrep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If pgrep itself isn't installed (rare), don't crash."""
    import subprocess

    from extensions.telegram import webhook_helper

    def fake_run(*_, **__):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert webhook_helper.detect_cloudflared_running() is False
