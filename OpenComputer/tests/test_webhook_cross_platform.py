"""Webhook cross_platform mode (PR 4.5).

cross_platform=true makes a webhook token "ferry text from platform X
to platform Y" without invoking the agent. It shares plumbing with
deliver_only (per PR 3c.5) but expresses the bridging intent
explicitly. Per spec:

1. POST with cross_platform=true → outgoing_queue.enqueue called
   with the rendered template body.
2. ``{{key}}`` substitution applied (delegates to _render_prompt).
3. Missing delivery_target rejected at startup
   (_validate_deliver_only_tokens → False).
4. Template can live nested in delivery_target (cross_platform
   convention) OR at the top level (deliver_only convention).
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


@pytest.fixture
async def adapter_cross_platform(tmp_path: Path):
    tokens_mod = _load_module("webhook_tokens_xp", _PLUGIN_DIR / "tokens.py")
    adapter_mod = _load_module("webhook_adapter_xp", _PLUGIN_DIR / "adapter.py")

    token_id, secret = tokens_mod.create_token(
        name="bridge-token", scopes=[], notify=None
    )
    tokens = tokens_mod.load_tokens()
    tokens[token_id]["cross_platform"] = True
    tokens[token_id]["delivery_target"] = {
        "platform": "telegram",
        "chat_id": "999",
        "template": "[bridge] {{user}}: {{text}}",
    }
    tokens_mod.save_tokens(tokens)

    a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
    fake_queue = MagicMock()
    fake_queue.enqueue = MagicMock(return_value=MagicMock(id="msg-bridge"))
    fake_api = MagicMock()
    fake_api.channels = {"telegram": MagicMock(), "webhook": a}
    fake_api.outgoing_queue = fake_queue
    a.bind_plugin_api(fake_api)

    a._app = aiohttp.web.Application(
        client_max_size=adapter_mod.WebhookAdapter.MAX_BODY_BYTES
    )
    a._app.router.add_post("/webhook/{token_id}", a._handle_webhook)

    handler_invocations: list = []

    async def sentinel(_event):
        handler_invocations.append(_event)

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
        "handler_invocations": handler_invocations,
    }
    await server.close()


# ---------------------------------------------------------------------------
# 1. POST with cross_platform=true → enqueue + template substitution
# ---------------------------------------------------------------------------


class TestCrossPlatformHappyPath:
    @pytest.mark.asyncio
    async def test_post_enqueues_rendered_template(
        self, adapter_cross_platform
    ) -> None:
        ctx = adapter_cross_platform
        body = json.dumps({"user": "alice", "text": "hi from slack"}).encode()
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
            assert data["chat_id"] == "999"

        ctx["queue"].enqueue.assert_called_once()
        kwargs = ctx["queue"].enqueue.call_args.kwargs
        assert kwargs["platform"] == "telegram"
        assert kwargs["chat_id"] == "999"
        assert kwargs["body"] == "[bridge] alice: hi from slack"
        # Source stamp distinguishes cross_platform from deliver_only.
        assert kwargs["metadata"]["source"] == "webhook_cross_platform"

    @pytest.mark.asyncio
    async def test_agent_handler_not_invoked(
        self, adapter_cross_platform
    ) -> None:
        ctx = adapter_cross_platform
        body = json.dumps({"user": "bob", "text": "ping"}).encode()
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
        # cross_platform bypasses the agent.
        assert ctx["handler_invocations"] == []


# ---------------------------------------------------------------------------
# 2. Validation: missing delivery_target rejected at startup
# ---------------------------------------------------------------------------


class TestCrossPlatformValidation:
    @pytest.mark.asyncio
    async def test_missing_delivery_target_rejected(self, tmp_path: Path) -> None:
        tokens_mod = _load_module("webhook_tokens_xpv1", _PLUGIN_DIR / "tokens.py")
        adapter_mod = _load_module("webhook_adapter_xpv1", _PLUGIN_DIR / "adapter.py")

        token_id, _ = tokens_mod.create_token(name="bad", scopes=[], notify=None)
        tokens = tokens_mod.load_tokens()
        tokens[token_id]["cross_platform"] = True
        # delivery_target absent
        tokens_mod.save_tokens(tokens)

        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        fake_api = MagicMock()
        fake_api.channels = {"telegram": MagicMock()}
        fake_api.outgoing_queue = MagicMock()
        a.bind_plugin_api(fake_api)

        ok = a._validate_deliver_only_tokens()
        assert ok is False

    @pytest.mark.asyncio
    async def test_unknown_platform_rejected(self, tmp_path: Path) -> None:
        tokens_mod = _load_module("webhook_tokens_xpv2", _PLUGIN_DIR / "tokens.py")
        adapter_mod = _load_module("webhook_adapter_xpv2", _PLUGIN_DIR / "adapter.py")

        token_id, _ = tokens_mod.create_token(name="bad2", scopes=[], notify=None)
        tokens = tokens_mod.load_tokens()
        tokens[token_id]["cross_platform"] = True
        tokens[token_id]["delivery_target"] = {
            "platform": "not-real",
            "chat_id": "x",
        }
        tokens_mod.save_tokens(tokens)

        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        fake_api = MagicMock()
        fake_api.channels = {"telegram": MagicMock()}
        fake_api.outgoing_queue = MagicMock()
        a.bind_plugin_api(fake_api)

        ok = a._validate_deliver_only_tokens()
        assert ok is False


# ---------------------------------------------------------------------------
# 3. Template lookup: nested delivery_target.template wins, top-level
# remains as deliver_only fallback for back-compat.
# ---------------------------------------------------------------------------


class TestTemplatePlacement:
    @pytest.mark.asyncio
    async def test_nested_template_used(self, tmp_path: Path) -> None:
        tokens_mod = _load_module("webhook_tokens_tpl1", _PLUGIN_DIR / "tokens.py")
        adapter_mod = _load_module("webhook_adapter_tpl1", _PLUGIN_DIR / "adapter.py")

        token_id, secret = tokens_mod.create_token(
            name="tpl", scopes=[], notify=None
        )
        tokens = tokens_mod.load_tokens()
        tokens[token_id]["cross_platform"] = True
        tokens[token_id]["delivery_target"] = {
            "platform": "telegram",
            "chat_id": "1",
            "template": "NESTED {{x}}",
        }
        # Top-level template must be IGNORED when nested one is set.
        tokens[token_id]["template"] = "TOP {{x}}"
        tokens_mod.save_tokens(tokens)

        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        fake_queue = MagicMock()
        fake_queue.enqueue = MagicMock(return_value=MagicMock(id="m"))
        fake_api = MagicMock()
        fake_api.channels = {"telegram": MagicMock()}
        fake_api.outgoing_queue = fake_queue
        a.bind_plugin_api(fake_api)
        a._app = aiohttp.web.Application(
            client_max_size=adapter_mod.WebhookAdapter.MAX_BODY_BYTES
        )
        a._app.router.add_post("/webhook/{token_id}", a._handle_webhook)
        a.set_message_handler(lambda *_: None)
        from aiohttp.test_utils import TestServer

        server = TestServer(a._app)
        await server.start_server()
        try:
            body = json.dumps({"x": "value"}).encode()
            headers = {
                "X-Webhook-Signature": _sign(body, secret),
                "Content-Type": "application/json",
            }
            async with aiohttp.ClientSession() as session, session.post(
                f"http://{server.host}:{server.port}/webhook/{token_id}",
                data=body,
                headers=headers,
            ) as resp:
                assert resp.status == 200
            kwargs = fake_queue.enqueue.call_args.kwargs
            assert kwargs["body"] == "NESTED value"
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_top_level_template_fallback(self, tmp_path: Path) -> None:
        """Back-compat: deliver_only tokens with top-level template
        still work (PR 3c.5 contract preserved)."""
        tokens_mod = _load_module("webhook_tokens_tpl2", _PLUGIN_DIR / "tokens.py")
        adapter_mod = _load_module("webhook_adapter_tpl2", _PLUGIN_DIR / "adapter.py")

        token_id, secret = tokens_mod.create_token(
            name="tpl-old", scopes=[], notify=None
        )
        tokens = tokens_mod.load_tokens()
        # cross_platform but only top-level template — should still
        # render via the fallback path.
        tokens[token_id]["cross_platform"] = True
        tokens[token_id]["delivery_target"] = {
            "platform": "telegram",
            "chat_id": "1",
        }
        tokens[token_id]["template"] = "TOP {{x}}"
        tokens_mod.save_tokens(tokens)

        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        fake_queue = MagicMock()
        fake_queue.enqueue = MagicMock(return_value=MagicMock(id="m"))
        fake_api = MagicMock()
        fake_api.channels = {"telegram": MagicMock()}
        fake_api.outgoing_queue = fake_queue
        a.bind_plugin_api(fake_api)
        a._app = aiohttp.web.Application(
            client_max_size=adapter_mod.WebhookAdapter.MAX_BODY_BYTES
        )
        a._app.router.add_post("/webhook/{token_id}", a._handle_webhook)
        a.set_message_handler(lambda *_: None)
        from aiohttp.test_utils import TestServer

        server = TestServer(a._app)
        await server.start_server()
        try:
            body = json.dumps({"x": "fallback"}).encode()
            headers = {
                "X-Webhook-Signature": _sign(body, secret),
                "Content-Type": "application/json",
            }
            async with aiohttp.ClientSession() as session, session.post(
                f"http://{server.host}:{server.port}/webhook/{token_id}",
                data=body,
                headers=headers,
            ) as resp:
                assert resp.status == 200
            kwargs = fake_queue.enqueue.call_args.kwargs
            assert kwargs["body"] == "TOP fallback"
        finally:
            await server.close()


# ---------------------------------------------------------------------------
# 4. _delivery_mode classifier
# ---------------------------------------------------------------------------


class TestDeliveryModeClassifier:
    def test_neither_returns_none(self) -> None:
        adapter_mod = _load_module(
            "webhook_adapter_dm1", _PLUGIN_DIR / "adapter.py"
        )
        assert adapter_mod.WebhookAdapter._delivery_mode({}) is None

    def test_deliver_only(self) -> None:
        adapter_mod = _load_module(
            "webhook_adapter_dm2", _PLUGIN_DIR / "adapter.py"
        )
        assert (
            adapter_mod.WebhookAdapter._delivery_mode({"deliver_only": True})
            == "deliver_only"
        )

    def test_cross_platform(self) -> None:
        adapter_mod = _load_module(
            "webhook_adapter_dm3", _PLUGIN_DIR / "adapter.py"
        )
        assert (
            adapter_mod.WebhookAdapter._delivery_mode(
                {"cross_platform": True}
            )
            == "cross_platform"
        )

    def test_cross_platform_wins_over_deliver_only(self) -> None:
        """If both flags are set the more-explicit cross_platform
        label wins for metadata stamping."""
        adapter_mod = _load_module(
            "webhook_adapter_dm4", _PLUGIN_DIR / "adapter.py"
        )
        meta = {"cross_platform": True, "deliver_only": True}
        assert (
            adapter_mod.WebhookAdapter._delivery_mode(meta)
            == "cross_platform"
        )
