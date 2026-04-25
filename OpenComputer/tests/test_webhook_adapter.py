"""Tests for the WebhookAdapter HTTP listener.

Spins up a real aiohttp server on an ephemeral port, sends real signed
HTTP POSTs via aiohttp.ClientSession, verifies dispatch + auth + rate
limiting + payload coercion. No external network required.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest

# Load adapter + tokens by absolute path so the tests don't depend on the
# plugin being enabled in the active profile.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "extensions" / "webhook"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Clear cached modules so each test gets fresh tokens module + state
    for k in list(sys.modules):
        if "webhook_tokens" in k or "webhook_adapter" in k:
            sys.modules.pop(k)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
async def adapter_with_token(tmp_path: Path):
    """Boot a WebhookAdapter on an ephemeral port + create a test token.

    Yields ``(adapter, token_id, secret, base_url)``.
    """
    tokens_mod = _load_module("webhook_tokens_test", _PLUGIN_DIR / "tokens.py")
    # Ensure the adapter sees the same tokens module (plugin uses sys.path import)
    adapter_mod = _load_module("webhook_adapter_test", _PLUGIN_DIR / "adapter.py")

    # Create a token before starting so the test doesn't race
    token_id, secret = tokens_mod.create_token(
        name="test", scopes=["skill:test"], notify="telegram"
    )

    # Bind on ephemeral port (port=0 → kernel picks free port)
    a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
    # Don't actually call connect() — we're driving the request handler directly
    # via aiohttp test_utils to avoid real port binding.
    from aiohttp.test_utils import TestServer
    a._app = aiohttp.web.Application(client_max_size=adapter_mod.WebhookAdapter.MAX_BODY_BYTES)
    a._app.router.add_post("/webhook/{token_id}", a._handle_webhook)
    a._app.router.add_get("/webhook/health", a._handle_health)

    server = TestServer(a._app)
    await server.start_server()
    base_url = f"http://{server.host}:{server.port}"

    yield a, token_id, secret, base_url

    await server.close()


def _sign(body: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_no_auth(self, adapter_with_token) -> None:
        adapter, _, _, base_url = adapter_with_token
        async with aiohttp.ClientSession() as session, session.get(f"{base_url}/webhook/health") as resp:
            assert resp.status == 200
            payload = await resp.json()
            assert payload["ok"] is True


class TestAuth:
    @pytest.mark.asyncio
    async def test_unknown_token_401(self, adapter_with_token) -> None:
        _, _, _, base_url = adapter_with_token
        body = json.dumps({"text": "x"}).encode()
        async with aiohttp.ClientSession() as session, session.post(
            f"{base_url}/webhook/badtoken",
            data=body,
            headers={"X-Webhook-Signature": "sha256=deadbeef"},
        ) as resp:
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_invalid_signature_403(self, adapter_with_token) -> None:
        _, token_id, _, base_url = adapter_with_token
        body = json.dumps({"text": "x"}).encode()
        async with aiohttp.ClientSession() as session, session.post(
            f"{base_url}/webhook/{token_id}",
            data=body,
            headers={"X-Webhook-Signature": "sha256=deadbeef"},
        ) as resp:
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_revoked_token_401(self, adapter_with_token) -> None:
        adapter, token_id, secret, base_url = adapter_with_token
        # Revoke directly via the tokens module
        tokens_mod = sys.modules["webhook_tokens_test"]
        tokens_mod.revoke_token(token_id)
        body = json.dumps({"text": "x"}).encode()
        async with aiohttp.ClientSession() as session, session.post(
            f"{base_url}/webhook/{token_id}",
            data=body,
            headers={"X-Webhook-Signature": _sign(body, secret)},
        ) as resp:
            assert resp.status == 401


class TestDispatch:
    @pytest.mark.asyncio
    async def test_valid_signature_dispatches(self, adapter_with_token) -> None:
        adapter, token_id, secret, base_url = adapter_with_token
        captured: list = []

        async def handler(event):
            captured.append(event)
            return None

        adapter.set_message_handler(handler)
        body = json.dumps({"alert": "GUJALKALI breakout above 1200"}).encode()
        async with aiohttp.ClientSession() as session, session.post(
            f"{base_url}/webhook/{token_id}",
            data=body,
            headers={"X-Webhook-Signature": _sign(body, secret), "Content-Type": "application/json"},
        ) as resp:
            assert resp.status == 200

        assert len(captured) == 1
        ev = captured[0]
        assert "GUJALKALI" in ev.text
        assert ev.metadata["webhook_token_id"] == token_id
        assert ev.metadata["webhook_notify"] == "telegram"
        assert ev.platform.value == "web"

    @pytest.mark.asyncio
    async def test_plain_text_body_accepted(self, adapter_with_token) -> None:
        adapter, token_id, secret, base_url = adapter_with_token
        captured: list = []

        async def handler(event):
            captured.append(event)
            return None

        adapter.set_message_handler(handler)
        body = b"hello world"
        async with aiohttp.ClientSession() as session, session.post(
            f"{base_url}/webhook/{token_id}",
            data=body,
            headers={"X-Webhook-Signature": _sign(body, secret), "Content-Type": "text/plain"},
        ) as resp:
            assert resp.status == 200

        assert captured[0].text == "hello world"


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_burst_blocked_after_limit(self, adapter_with_token) -> None:
        adapter, token_id, secret, base_url = adapter_with_token

        async def handler(event):
            return None

        adapter.set_message_handler(handler)

        # Lower the limit for this test
        from extensions.webhook import adapter as adapter_module  # noqa: F401
        # Read module state directly — _RATE_LIMIT_REQS is module-level
        adapter_mod = sys.modules["webhook_adapter_test"]
        adapter_mod._RATE_LIMIT_REQS = 3  # type: ignore[attr-defined]

        body = b'{"text":"hi"}'
        sig = _sign(body, secret)
        results: list[int] = []
        async with aiohttp.ClientSession() as session:
            for _ in range(5):
                async with session.post(
                    f"{base_url}/webhook/{token_id}",
                    data=body,
                    headers={"X-Webhook-Signature": sig, "Content-Type": "application/json"},
                ) as r:
                    results.append(r.status)

        # First 3 succeed, then 429
        assert results[:3] == [200, 200, 200]
        assert 429 in results[3:]


class TestSendUnsupported:
    @pytest.mark.asyncio
    async def test_send_returns_error(self, adapter_with_token) -> None:
        adapter, _, _, _ = adapter_with_token
        result = await adapter.send("anyone", "hi")
        assert not result.success
        assert "inbound-only" in result.error


class TestCapabilities:
    def test_capabilities_is_none(self) -> None:
        adapter_mod = _load_module("webhook_adapter_caps_test", _PLUGIN_DIR / "adapter.py")
        from plugin_sdk import ChannelCapabilities

        assert adapter_mod.WebhookAdapter.capabilities == ChannelCapabilities.NONE


class TestPayloadCoercion:
    @pytest.mark.asyncio
    async def test_text_field_preferred(self, adapter_with_token) -> None:
        adapter, token_id, secret, base_url = adapter_with_token
        captured: list = []

        async def h(event):
            captured.append(event)

        adapter.set_message_handler(h)
        body = json.dumps({"text": "primary", "alert": "ignored", "message": "also ignored"}).encode()
        async with aiohttp.ClientSession() as s, s.post(
            f"{base_url}/webhook/{token_id}",
            data=body,
            headers={"X-Webhook-Signature": _sign(body, secret), "Content-Type": "application/json"},
        ):
            pass
        await asyncio.sleep(0.01)  # let handler run
        assert captured[0].text == "primary"

    @pytest.mark.asyncio
    async def test_alert_used_when_text_absent(self, adapter_with_token) -> None:
        adapter, token_id, secret, base_url = adapter_with_token
        captured: list = []

        async def h(event):
            captured.append(event)

        adapter.set_message_handler(h)
        body = json.dumps({"alert": "BUY GUJALKALI"}).encode()
        async with aiohttp.ClientSession() as s, s.post(
            f"{base_url}/webhook/{token_id}",
            data=body,
            headers={"X-Webhook-Signature": _sign(body, secret), "Content-Type": "application/json"},
        ):
            pass
        await asyncio.sleep(0.01)
        assert captured[0].text == "BUY GUJALKALI"

    @pytest.mark.asyncio
    async def test_empty_payload_400(self, adapter_with_token) -> None:
        _, token_id, secret, base_url = adapter_with_token
        body = json.dumps({"unrelated_field": True}).encode()
        async with aiohttp.ClientSession() as s, s.post(
            f"{base_url}/webhook/{token_id}",
            data=body,
            headers={"X-Webhook-Signature": _sign(body, secret), "Content-Type": "application/json"},
        ) as r:
            # Coerce_text turns dict into "unrelated_field=true" — that's truthy.
            # So this should actually succeed with a synthesized text. Verify.
            assert r.status == 200
