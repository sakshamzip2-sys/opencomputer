"""Tests for the WebhookInboundAdapter — aiohttp server end-to-end.

Spins up the adapter on an ephemeral port, sends signed POSTs at each
platform path, asserts dispatch occurs (or 401 on bad signature).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import socket
import sys
from pathlib import Path

import aiohttp
import pytest

_REPO = Path(__file__).parent.parent
_ADAPTER_PY = _REPO / "extensions" / "webhook-inbound" / "adapter.py"


def _load_adapter():
    sys.modules.pop("webhook_inbound_adapter_test", None)
    spec = importlib.util.spec_from_file_location(
        "webhook_inbound_adapter_test", _ADAPTER_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webhook_inbound_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def isolated_token_store(tmp_path, monkeypatch):
    """Point the webhook tokens registry at a temp file + create one token."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # Create the default profile dir so create_token finds a home
    (tmp_path / "default").mkdir(exist_ok=True)
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "default")

    sys.modules.pop("webhook_inbound_adapter_test", None)
    sys.modules.pop("_webhook_inbound_tokens", None)
    mod = _load_adapter()
    return mod


@pytest.fixture
def adapter_with_token(isolated_token_store, free_port):
    """Adapter started on free_port with one token created for tests."""
    mod = isolated_token_store
    # Create a token via the imported tokens module
    tokens_mod = sys.modules["_webhook_inbound_tokens"]
    token_id, secret = tokens_mod.create_token(name="test")
    return mod, token_id, secret, free_port


class _StubAPI:
    def __init__(self):
        self.events = []

    async def dispatch_message(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok(adapter_with_token):
    mod, _, _, port = adapter_with_token
    adapter = mod.WebhookInboundAdapter(config={"host": "127.0.0.1", "port": port})
    api = _StubAPI()
    adapter.bind_plugin_api(api)
    assert await adapter.connect() is True
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/inbound/health") as r:
                assert r.status == 200
                assert await r.text() == "ok"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_teams_inbound_dispatches_with_valid_hmac(adapter_with_token):
    mod, token_id, secret, port = adapter_with_token
    adapter = mod.WebhookInboundAdapter(config={"host": "127.0.0.1", "port": port})
    api = _StubAPI()
    adapter.bind_plugin_api(api)
    assert await adapter.connect() is True

    body_dict = {"text": "@bot hi", "from": {"name": "Alice", "id": "u-1"}}
    body_bytes = json.dumps(body_dict).encode()
    # Teams secret is base64 — but our token store uses hex. The adapter calls
    # the verifier with the stored secret as-is; the verifier base64-decodes it
    # to derive the HMAC key. So we must stash a base64 secret in the token.
    # For this test, override by using a base64-encoded version of the hex secret.
    secret_b64 = base64.b64encode(secret.encode()).decode()
    # Re-store the token with the b64 secret so verify_teams can decode it
    tokens_mod = sys.modules["_webhook_inbound_tokens"]
    tokens = tokens_mod.load_tokens()
    tokens[token_id]["secret"] = secret_b64
    tokens_mod.save_tokens(tokens)

    expected_digest = hmac.new(
        base64.b64decode(secret_b64), body_bytes, hashlib.sha256
    ).digest()
    auth = f"HMAC {base64.b64encode(expected_digest).decode()}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/inbound/teams/{token_id}",
                data=body_bytes,
                headers={
                    "Authorization": auth,
                    "Content-Type": "application/json",
                },
            ) as r:
                assert r.status == 200, await r.text()
        assert len(api.events) == 1
        event = api.events[0]
        assert event.text == "@bot hi"
        assert event.metadata["inbound_platform"] == "teams"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_teams_rejects_bad_hmac(adapter_with_token):
    mod, token_id, secret, port = adapter_with_token
    adapter = mod.WebhookInboundAdapter(config={"host": "127.0.0.1", "port": port})
    adapter.bind_plugin_api(_StubAPI())
    assert await adapter.connect() is True

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/inbound/teams/{token_id}",
                json={"text": "x"},
                headers={"Authorization": "HMAC bogus"},
            ) as r:
                assert r.status == 401
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_dingtalk_inbound_dispatches(adapter_with_token):
    mod, token_id, secret, port = adapter_with_token
    adapter = mod.WebhookInboundAdapter(config={"host": "127.0.0.1", "port": port})
    api = _StubAPI()
    adapter.bind_plugin_api(api)
    assert await adapter.connect() is True

    timestamp = "1700000000000"
    string_to_sign = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(secret.encode(), string_to_sign, hashlib.sha256).digest()
    sign = base64.b64encode(digest).decode()

    body = {
        "msgtype": "text",
        "text": {"content": "@bot hello"},
        "senderNick": "Bob",
        "senderId": "u-2",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/inbound/dingtalk/{token_id}",
                json=body,
                headers={"timestamp": timestamp, "sign": sign},
            ) as r:
                assert r.status == 200, await r.text()
        assert len(api.events) == 1
        assert api.events[0].text == "@bot hello"
        assert api.events[0].metadata["inbound_platform"] == "dingtalk"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_feishu_url_verification_echoes_challenge(adapter_with_token):
    mod, token_id, _, port = adapter_with_token
    adapter = mod.WebhookInboundAdapter(config={"host": "127.0.0.1", "port": port})
    adapter.bind_plugin_api(_StubAPI())
    assert await adapter.connect() is True

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/inbound/feishu/{token_id}",
                json={"type": "url_verification", "challenge": "challenge-xyz"},
            ) as r:
                assert r.status == 200
                data = await r.json()
                assert data["challenge"] == "challenge-xyz"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_feishu_event_dispatches_on_valid_signature(adapter_with_token):
    mod, token_id, secret, port = adapter_with_token
    adapter = mod.WebhookInboundAdapter(config={"host": "127.0.0.1", "port": port})
    api = _StubAPI()
    adapter.bind_plugin_api(api)
    assert await adapter.connect() is True

    timestamp = "1700000000"
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode(), b"", hashlib.sha256).digest()
    sign = base64.b64encode(digest).decode()

    body = {
        "type": "event_callback",
        "event": {
            "message": {"content": '{"text":"@bot help"}'},
            "sender": {"sender_id": {"open_id": "ou-1"}},
        },
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/inbound/feishu/{token_id}",
                json=body,
                headers={
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Signature": sign,
                },
            ) as r:
                assert r.status == 200, await r.text()
        assert len(api.events) == 1
        assert api.events[0].text == "@bot help"
        assert api.events[0].metadata["inbound_platform"] == "feishu"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_unknown_token_returns_401(adapter_with_token):
    mod, _, _, port = adapter_with_token
    adapter = mod.WebhookInboundAdapter(config={"host": "127.0.0.1", "port": port})
    adapter.bind_plugin_api(_StubAPI())
    assert await adapter.connect() is True

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/inbound/teams/unknown-token-id",
                json={"text": "x"},
                headers={"Authorization": "HMAC anything"},
            ) as r:
                assert r.status == 401
    finally:
        await adapter.disconnect()


def test_send_returns_failure_indicating_inbound_only(isolated_token_store):
    mod = isolated_token_store
    adapter = mod.WebhookInboundAdapter(config={})
    result = asyncio.run(adapter.send(chat_id="x", text="x"))
    assert result.success is False
    assert "inbound-only" in (result.error or "")


def test_plugin_manifest_exists():
    manifest_path = (
        _REPO / "extensions" / "webhook-inbound" / "plugin.json"
    )
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["kind"] == "channel"
    assert manifest["entry"] == "plugin"
