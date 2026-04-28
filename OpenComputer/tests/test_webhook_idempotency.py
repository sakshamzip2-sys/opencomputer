"""Webhook idempotency cache (PR 4.4).

Providers retry deliveries (GitHub, Stripe, runaway external
schedulers). The webhook adapter de-duplicates within a 1h window:

1. Header preference: X-Github-Delivery → X-Delivery-ID →
   X-Idempotency-Key → Stripe-Signature → sha256(body+token_id).
2. First POST → dispatched.
3. Same delivery_id within TTL → 200 with
   ``{"status":"duplicate","first_seen":ts}`` and NO dispatch.
4. After TTL → re-dispatched.
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
async def adapter_with_token(tmp_path: Path):
    tokens_mod = _load_module("webhook_tokens_idem", _PLUGIN_DIR / "tokens.py")
    adapter_mod = _load_module("webhook_adapter_idem", _PLUGIN_DIR / "adapter.py")

    token_id, secret = tokens_mod.create_token(
        name="idempotency-test", scopes=[], notify=None
    )

    a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
    a._app = aiohttp.web.Application(
        client_max_size=adapter_mod.WebhookAdapter.MAX_BODY_BYTES
    )
    a._app.router.add_post("/webhook/{token_id}", a._handle_webhook)

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
        "token_id": token_id,
        "secret": secret,
        "base_url": base_url,
        "handler_invocations": handler_invocations,
    }
    await server.close()


# ---------------------------------------------------------------------------
# _delivery_id helper: header preference + fallback
# ---------------------------------------------------------------------------


class TestDeliveryId:
    def test_github_delivery_header_preferred(self) -> None:
        adapter_mod = _load_module(
            "webhook_idem_did1", _PLUGIN_DIR / "adapter.py"
        )
        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        request = MagicMock()
        request.headers = {
            "X-Github-Delivery": "abc-123",
            "X-Delivery-ID": "would-lose",
            "X-Idempotency-Key": "would-lose-too",
        }
        out = a._delivery_id(request, b"body", "tok")
        assert "X-Github-Delivery" in out
        assert "abc-123" in out

    def test_x_delivery_id_used_when_github_absent(self) -> None:
        adapter_mod = _load_module(
            "webhook_idem_did2", _PLUGIN_DIR / "adapter.py"
        )
        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        request = MagicMock()
        request.headers = {"X-Delivery-ID": "delivery-xyz"}
        out = a._delivery_id(request, b"body", "tok")
        assert "X-Delivery-ID" in out
        assert "delivery-xyz" in out

    def test_stripe_signature_picked_up(self) -> None:
        adapter_mod = _load_module(
            "webhook_idem_did3", _PLUGIN_DIR / "adapter.py"
        )
        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        request = MagicMock()
        request.headers = {"Stripe-Signature": "t=123,v1=abc"}
        out = a._delivery_id(request, b"body", "tok")
        assert "Stripe-Signature" in out
        assert "t=123,v1=abc" in out

    def test_fallback_sha256_when_no_header(self) -> None:
        adapter_mod = _load_module(
            "webhook_idem_did4", _PLUGIN_DIR / "adapter.py"
        )
        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        request = MagicMock()
        request.headers = {}
        out1 = a._delivery_id(request, b"body-A", "tok-1")
        out2 = a._delivery_id(request, b"body-A", "tok-1")
        out3 = a._delivery_id(request, b"body-B", "tok-1")
        out4 = a._delivery_id(request, b"body-A", "tok-2")
        assert out1.startswith("sha256:")
        assert out1 == out2  # determinism
        assert out1 != out3  # body-sensitive
        assert out1 != out4  # token-id-sensitive

    def test_idempotency_check_records_then_duplicates(self) -> None:
        adapter_mod = _load_module(
            "webhook_idem_check", _PLUGIN_DIR / "adapter.py"
        )
        a = adapter_mod.WebhookAdapter({"host": "127.0.0.1", "port": 0})
        # First call: not seen → returns None and records.
        first = a._idempotency_check("tok", "did-1")
        assert first is None
        # Second call: returns the original timestamp.
        second = a._idempotency_check("tok", "did-1")
        assert second is not None
        assert isinstance(second, float)
        # Different delivery_id → not duplicate.
        third = a._idempotency_check("tok", "did-2")
        assert third is None
        # Different token → not duplicate.
        fourth = a._idempotency_check("tok-other", "did-1")
        assert fourth is None


# ---------------------------------------------------------------------------
# End-to-end POST behaviour
# ---------------------------------------------------------------------------


class TestIdempotencyEndToEnd:
    @pytest.mark.asyncio
    async def test_first_post_dispatched(self, adapter_with_token) -> None:
        ctx = adapter_with_token
        body = json.dumps({"text": "hello"}).encode()
        headers = {
            "X-Webhook-Signature": _sign(body, ctx["secret"]),
            "Content-Type": "application/json",
            "X-Github-Delivery": "delivery-001",
        }
        async with aiohttp.ClientSession() as session, session.post(
            f"{ctx['base_url']}/webhook/{ctx['token_id']}",
            data=body,
            headers=headers,
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data.get("ok") is True
        assert len(ctx["handler_invocations"]) == 1

    @pytest.mark.asyncio
    async def test_same_delivery_id_returns_duplicate(
        self, adapter_with_token
    ) -> None:
        ctx = adapter_with_token
        body = json.dumps({"text": "hello"}).encode()
        sig = _sign(body, ctx["secret"])
        headers = {
            "X-Webhook-Signature": sig,
            "Content-Type": "application/json",
            "X-Github-Delivery": "delivery-002",
        }

        async with aiohttp.ClientSession() as session:
            # First POST — dispatched.
            async with session.post(
                f"{ctx['base_url']}/webhook/{ctx['token_id']}",
                data=body,
                headers=headers,
            ) as resp:
                assert resp.status == 200
                first_data = await resp.json()
                assert first_data.get("ok") is True
            # Second POST with same delivery_id — duplicate, no dispatch.
            async with session.post(
                f"{ctx['base_url']}/webhook/{ctx['token_id']}",
                data=body,
                headers=headers,
            ) as resp:
                assert resp.status == 200
                second_data = await resp.json()
                assert second_data["status"] == "duplicate"
                assert "first_seen" in second_data

        # Handler invoked exactly once — duplicate skipped dispatch.
        assert len(ctx["handler_invocations"]) == 1

    @pytest.mark.asyncio
    async def test_different_delivery_ids_both_dispatched(
        self, adapter_with_token
    ) -> None:
        ctx = adapter_with_token
        body = json.dumps({"text": "hello"}).encode()
        sig = _sign(body, ctx["secret"])

        async with aiohttp.ClientSession() as session:
            for did in ("delivery-A", "delivery-B"):
                headers = {
                    "X-Webhook-Signature": sig,
                    "Content-Type": "application/json",
                    "X-Github-Delivery": did,
                }
                async with session.post(
                    f"{ctx['base_url']}/webhook/{ctx['token_id']}",
                    data=body,
                    headers=headers,
                ) as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data.get("ok") is True

        assert len(ctx["handler_invocations"]) == 2

    @pytest.mark.asyncio
    async def test_after_ttl_re_dispatches(self, adapter_with_token) -> None:
        """Force the cache entry past TTL by mutating the recorded ts.

        We don't sleep an hour — instead, we reach into the adapter's
        cache and rewind the recorded timestamp so the lazy-purge path
        evicts the entry on the next check.
        """
        ctx = adapter_with_token
        body = json.dumps({"text": "hello"}).encode()
        sig = _sign(body, ctx["secret"])
        headers = {
            "X-Webhook-Signature": sig,
            "Content-Type": "application/json",
            "X-Github-Delivery": "delivery-ttl",
        }

        async with aiohttp.ClientSession() as session, session.post(
            f"{ctx['base_url']}/webhook/{ctx['token_id']}",
            data=body,
            headers=headers,
        ) as resp:
            assert resp.status == 200

        assert len(ctx["handler_invocations"]) == 1

        # Rewind every recorded timestamp 2h into the past.
        bucket = ctx["adapter"]._seen_deliveries[ctx["token_id"]]
        for k in list(bucket.keys()):
            bucket[k] -= 7200.0

        async with aiohttp.ClientSession() as session, session.post(
            f"{ctx['base_url']}/webhook/{ctx['token_id']}",
            data=body,
            headers=headers,
        ) as resp:
            # Lazy-purge sweeps the stale entry → re-dispatched.
            assert resp.status == 200
            data = await resp.json()
            assert data.get("ok") is True

        assert len(ctx["handler_invocations"]) == 2

    @pytest.mark.asyncio
    async def test_fallback_hash_dedupes_byte_identical_retries(
        self, adapter_with_token
    ) -> None:
        """No idempotency header → byte-identical body still
        de-duplicates via the sha256(body+token_id) fallback."""
        ctx = adapter_with_token
        body = json.dumps({"text": "hello-from-no-header"}).encode()
        headers = {
            "X-Webhook-Signature": _sign(body, ctx["secret"]),
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            for _ in range(2):
                async with session.post(
                    f"{ctx['base_url']}/webhook/{ctx['token_id']}",
                    data=body,
                    headers=headers,
                ) as resp:
                    assert resp.status == 200
        assert len(ctx["handler_invocations"]) == 1
