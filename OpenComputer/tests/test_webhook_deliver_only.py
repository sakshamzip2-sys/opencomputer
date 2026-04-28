"""Webhook ``deliver_only`` mode (PR 3c.5).

Tokens may be configured with ``deliver_only: true`` plus a
``delivery_target: {platform, chat_id}`` and an optional template. POSTs
to such tokens skip the agent entirely — the adapter renders the
template against the JSON payload and enqueues the result on
``api.outgoing_queue`` for delivery to the named platform/chat.

Use case: external services that already produce a final user-facing
string (UptimeRobot incident, GitHub Action build failure with custom
message, TradingView "send the alert verbatim"). Routing through the
agent would burn tokens for zero added value.

This test file covers:
1. ``_render_prompt`` substitution semantics (string keys, missing
   keys, non-dict payload).
2. POST with deliver_only=true → outgoing_queue.enqueue called with the
   rendered body and the configured platform/chat.
3. NO agent run is invoked — the message-handler attribute is never
   used in the deliver-only branch.
4. ``_validate_deliver_only_tokens`` refuses to start when a token's
   delivery_target.platform is not registered as a channel.
5. Missing ``outgoing_queue`` returns 503 instead of silently
   dropping the request.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest

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
    for k in list(sys.modules):
        if "webhook_tokens" in k or "webhook_adapter" in k:
            sys.modules.pop(k)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


def _sign(body: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


# ---------------------------------------------------------------------------
# 1. _render_prompt unit tests
# ---------------------------------------------------------------------------


class TestRenderPrompt:
    def test_simple_substitution(self) -> None:
        mod = _load_module("webhook_render_test", _PLUGIN_DIR / "adapter.py")
        out = mod._render_prompt("Hello {{name}}!", {"name": "Alice"})
        assert out == "Hello Alice!"

    def test_multiple_keys(self) -> None:
        mod = _load_module("webhook_render_test2", _PLUGIN_DIR / "adapter.py")
        out = mod._render_prompt(
            "{{ticker}} crossed {{level}}", {"ticker": "AAPL", "level": "200"}
        )
        assert out == "AAPL crossed 200"

    def test_whitespace_in_braces_allowed(self) -> None:
        mod = _load_module("webhook_render_test3", _PLUGIN_DIR / "adapter.py")
        out = mod._render_prompt("hi {{ name }}", {"name": "Bob"})
        assert out == "hi Bob"

    def test_missing_key_renders_empty(self) -> None:
        mod = _load_module("webhook_render_test4", _PLUGIN_DIR / "adapter.py")
        out = mod._render_prompt("{{a}} - {{missing}}", {"a": "x"})
        assert out == "x - "

    def test_empty_template(self) -> None:
        mod = _load_module("webhook_render_test5", _PLUGIN_DIR / "adapter.py")
        assert mod._render_prompt("", {"a": 1}) == ""

    def test_non_dict_payload_uses_value(self) -> None:
        mod = _load_module("webhook_render_test6", _PLUGIN_DIR / "adapter.py")
        out = mod._render_prompt("got: {{value}}", "raw-string")
        assert out == "got: raw-string"

    def test_int_value_stringified(self) -> None:
        mod = _load_module("webhook_render_test7", _PLUGIN_DIR / "adapter.py")
        out = mod._render_prompt("count={{n}}", {"n": 42})
        assert out == "count=42"


# ---------------------------------------------------------------------------
# Test fixtures: spin up a real aiohttp server with a deliver_only token
# ---------------------------------------------------------------------------


@pytest.fixture
async def adapter_deliver_only(tmp_path: Path):
    tokens_mod = _load_module("webhook_tokens_d", _PLUGIN_DIR / "tokens.py")
    adapter_mod = _load_module("webhook_adapter_d", _PLUGIN_DIR / "adapter.py")

    token_id, secret = tokens_mod.create_token(
        name="tradingview-deliver", scopes=[], notify=None
    )
    # Patch the token to deliver_only mode + delivery_target.
    tokens = tokens_mod.load_tokens()
    tokens[token_id]["deliver_only"] = True
    tokens[token_id]["delivery_target"] = {
        "platform": "telegram",
        "chat_id": "12345",
    }
    tokens[token_id]["template"] = "{{ticker}}: {{action}}"
    tokens_mod.save_tokens(tokens)

    a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
    # Stub a PluginAPI: provides ``channels`` and ``outgoing_queue``.
    fake_queue = MagicMock()
    fake_queue.enqueue = MagicMock(return_value=MagicMock(id="msg-001"))

    fake_api = MagicMock()
    fake_api.channels = {"telegram": MagicMock(), "webhook": a}
    fake_api.outgoing_queue = fake_queue
    a.bind_plugin_api(fake_api)

    a._app = aiohttp.web.Application(client_max_size=adapter_mod.WebhookAdapter.MAX_BODY_BYTES)
    a._app.router.add_post("/webhook/{token_id}", a._handle_webhook)
    a._app.router.add_get("/webhook/health", a._handle_health)

    # Register a sentinel message handler that we can assert was NOT used.
    handler_invocations: list = []

    async def sentinel(_event):
        handler_invocations.append(_event)
        return None

    a.set_message_handler(sentinel)

    from aiohttp.test_utils import TestServer

    server = TestServer(a._app)
    await server.start_server()
    base_url = f"http://{server.host}:{server.port}"

    yield {
        "adapter": a,
        "adapter_mod": adapter_mod,
        "tokens_mod": tokens_mod,
        "token_id": token_id,
        "secret": secret,
        "base_url": base_url,
        "queue": fake_queue,
        "api": fake_api,
        "handler_invocations": handler_invocations,
    }
    await server.close()


# ---------------------------------------------------------------------------
# 2. POST → outgoing_queue.enqueue called, template applied
# ---------------------------------------------------------------------------


class TestDeliverOnlyHappyPath:
    @pytest.mark.asyncio
    async def test_post_enqueues_rendered_template(self, adapter_deliver_only) -> None:
        ctx = adapter_deliver_only
        body = json.dumps({"ticker": "AAPL", "action": "BUY"}).encode()
        headers = {
            "X-Webhook-Signature": _sign(body, ctx["secret"]),
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session, session.post(
            f"{ctx['base_url']}/webhook/{ctx['token_id']}",
            data=body,
            headers=headers,
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["queued"] is True
            assert data["platform"] == "telegram"
            assert data["chat_id"] == "12345"

        # outgoing_queue.enqueue called exactly once with the rendered body.
        ctx["queue"].enqueue.assert_called_once()
        kwargs = ctx["queue"].enqueue.call_args.kwargs
        assert kwargs["platform"] == "telegram"
        assert kwargs["chat_id"] == "12345"
        assert kwargs["body"] == "AAPL: BUY"
        assert kwargs["metadata"]["source"] == "webhook_deliver_only"

    @pytest.mark.asyncio
    async def test_post_does_not_invoke_agent_handler(
        self, adapter_deliver_only
    ) -> None:
        ctx = adapter_deliver_only
        body = json.dumps({"ticker": "TSLA", "action": "SELL"}).encode()
        headers = {
            "X-Webhook-Signature": _sign(body, ctx["secret"]),
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session, session.post(
            f"{ctx['base_url']}/webhook/{ctx['token_id']}",
            data=body,
            headers=headers,
        ) as resp:
            assert resp.status == 200

        # The sentinel message-handler must never fire — deliver_only
        # bypasses the agent entirely.
        assert ctx["handler_invocations"] == []


# ---------------------------------------------------------------------------
# 3. Validation: missing delivery_target rejected at startup
# ---------------------------------------------------------------------------


class TestValidationAtStartup:
    @pytest.mark.asyncio
    async def test_unknown_platform_refuses_to_start(self, tmp_path: Path) -> None:
        tokens_mod = _load_module("webhook_tokens_v1", _PLUGIN_DIR / "tokens.py")
        adapter_mod = _load_module("webhook_adapter_v1", _PLUGIN_DIR / "adapter.py")

        token_id, _ = tokens_mod.create_token(name="bad", scopes=[], notify=None)
        tokens = tokens_mod.load_tokens()
        tokens[token_id]["deliver_only"] = True
        tokens[token_id]["delivery_target"] = {
            "platform": "not-a-real-channel",
            "chat_id": "x",
        }
        tokens_mod.save_tokens(tokens)

        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        fake_api = MagicMock()
        fake_api.channels = {"telegram": MagicMock()}  # no "not-a-real-channel"
        fake_api.outgoing_queue = MagicMock()
        a.bind_plugin_api(fake_api)

        ok = a._validate_deliver_only_tokens()
        assert ok is False

    @pytest.mark.asyncio
    async def test_incomplete_delivery_target_rejected(self, tmp_path: Path) -> None:
        tokens_mod = _load_module("webhook_tokens_v2", _PLUGIN_DIR / "tokens.py")
        adapter_mod = _load_module("webhook_adapter_v2", _PLUGIN_DIR / "adapter.py")

        token_id, _ = tokens_mod.create_token(name="bad2", scopes=[], notify=None)
        tokens = tokens_mod.load_tokens()
        tokens[token_id]["deliver_only"] = True
        tokens[token_id]["delivery_target"] = {"platform": "telegram"}  # missing chat_id
        tokens_mod.save_tokens(tokens)

        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        fake_api = MagicMock()
        fake_api.channels = {"telegram": MagicMock()}
        fake_api.outgoing_queue = MagicMock()
        a.bind_plugin_api(fake_api)

        assert a._validate_deliver_only_tokens() is False

    @pytest.mark.asyncio
    async def test_valid_token_passes_validation(self, tmp_path: Path) -> None:
        tokens_mod = _load_module("webhook_tokens_v3", _PLUGIN_DIR / "tokens.py")
        adapter_mod = _load_module("webhook_adapter_v3", _PLUGIN_DIR / "adapter.py")

        token_id, _ = tokens_mod.create_token(name="good", scopes=[], notify=None)
        tokens = tokens_mod.load_tokens()
        tokens[token_id]["deliver_only"] = True
        tokens[token_id]["delivery_target"] = {
            "platform": "telegram",
            "chat_id": "1",
        }
        tokens_mod.save_tokens(tokens)

        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        fake_api = MagicMock()
        fake_api.channels = {"telegram": MagicMock()}
        fake_api.outgoing_queue = MagicMock()
        a.bind_plugin_api(fake_api)
        assert a._validate_deliver_only_tokens() is True

    @pytest.mark.asyncio
    async def test_non_deliver_only_tokens_pass_through(self, tmp_path: Path) -> None:
        tokens_mod = _load_module("webhook_tokens_v4", _PLUGIN_DIR / "tokens.py")
        adapter_mod = _load_module("webhook_adapter_v4", _PLUGIN_DIR / "adapter.py")

        # Plain agent-style token; no deliver_only fields. Validator
        # should not care about it.
        tokens_mod.create_token(name="plain", scopes=[], notify=None)

        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        fake_api = MagicMock()
        fake_api.channels = {}
        fake_api.outgoing_queue = MagicMock()
        a.bind_plugin_api(fake_api)
        assert a._validate_deliver_only_tokens() is True


# ---------------------------------------------------------------------------
# 4. Outgoing queue absent → 503 (don't silently drop)
# ---------------------------------------------------------------------------


class TestNoOutgoingQueue:
    @pytest.mark.asyncio
    async def test_no_queue_returns_503(self, tmp_path: Path) -> None:
        tokens_mod = _load_module("webhook_tokens_q", _PLUGIN_DIR / "tokens.py")
        adapter_mod = _load_module("webhook_adapter_q", _PLUGIN_DIR / "adapter.py")

        token_id, secret = tokens_mod.create_token(
            name="no-queue", scopes=[], notify=None
        )
        tokens = tokens_mod.load_tokens()
        tokens[token_id]["deliver_only"] = True
        tokens[token_id]["delivery_target"] = {
            "platform": "telegram",
            "chat_id": "1",
        }
        tokens[token_id]["template"] = "static body"
        tokens_mod.save_tokens(tokens)

        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        fake_api = MagicMock()
        fake_api.channels = {"telegram": MagicMock()}
        fake_api.outgoing_queue = None  # NOT bound
        a.bind_plugin_api(fake_api)

        a._app = aiohttp.web.Application(
            client_max_size=adapter_mod.WebhookAdapter.MAX_BODY_BYTES
        )
        a._app.router.add_post("/webhook/{token_id}", a._handle_webhook)

        from aiohttp.test_utils import TestServer

        server = TestServer(a._app)
        await server.start_server()
        try:
            body = json.dumps({"x": 1}).encode()
            headers = {
                "X-Webhook-Signature": _sign(body, secret),
                "Content-Type": "application/json",
            }
            async with aiohttp.ClientSession() as session, session.post(
                f"http://{server.host}:{server.port}/webhook/{token_id}",
                data=body,
                headers=headers,
            ) as resp:
                assert resp.status == 503
                data = await resp.json()
                assert "outgoing_queue" in data["error"]
        finally:
            await server.close()
