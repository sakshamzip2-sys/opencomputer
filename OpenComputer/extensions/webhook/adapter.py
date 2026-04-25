"""WebhookAdapter — generic HTTP webhook channel.

Listens on a configurable port for POST requests. Each token has its own
URL ``POST /webhook/<token_id>`` with HMAC-SHA256 auth via the
``X-Webhook-Signature`` header. Verified payloads are converted to
``MessageEvent`` and dispatched to the gateway.

Use cases:
- TradingView alert → POST → agent analyzes ticker → notify Telegram
- Zapier / n8n / IFTTT → POST → agent runs the configured skill
- GitHub Action → POST "build failed" → agent investigates
- External cron-like scheduler → POST → agent executes a task

Security model:
- Each token has a per-token HMAC secret (32 bytes). Secret shown once
  on creation, stored in ``<profile_home>/webhook_tokens.json``.
- Signature verification uses constant-time HMAC compare (timing-safe).
- ``client_max_size = 1 MB`` to bound payload size (prevents OOM via
  giant POST bodies).
- Token registry is file-mode 0600.

Self-audit verifications applied:
- R2: gateway has NO shared aiohttp server, so we bind our own port.
- R8 (security baseline): rate-limited via aiohttp middleware (60 req/min
  per token), input-size capped, auth verified before dispatch.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from aiohttp import web

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import MessageEvent, Platform, SendResult

# Plugin-loader mode (sys.path includes the plugin dir): plain import.
# Test/package mode: load tokens.py from sibling path.
try:
    from tokens import (  # plugin-loader mode  # type: ignore[import-not-found]
        get_token,
        mark_used,
        verify_signature,
    )
except ImportError:  # pragma: no cover — fallback for test / package mode
    import importlib.util
    import sys
    from pathlib import Path as _Path

    _spec = importlib.util.spec_from_file_location(
        "_webhook_tokens_local",
        _Path(__file__).resolve().parent / "tokens.py",
    )
    if _spec is None or _spec.loader is None:
        raise
    _tokens_mod = importlib.util.module_from_spec(_spec)
    sys.modules["_webhook_tokens_local"] = _tokens_mod
    _spec.loader.exec_module(_tokens_mod)
    get_token = _tokens_mod.get_token
    mark_used = _tokens_mod.mark_used
    verify_signature = _tokens_mod.verify_signature

logger = logging.getLogger("opencomputer.ext.webhook")


# Per-token rate limit. 60 POSTs/min/token is generous for normal triggers
# (TradingView alerts fire ≤1/min) but blocks runaway clients.
_RATE_LIMIT_REQS = 60
_RATE_LIMIT_WINDOW_SECONDS = 60.0


class WebhookAdapter(BaseChannelAdapter):
    platform = Platform.WEB
    max_message_length = 64_000  # webhook responses can be larger than chat
    capabilities = ChannelCapabilities.NONE  # no typing, no reactions, no edit

    # Telegram doesn't model THREADS the way Discord/Slack do — webhook
    # is one-shot triggers, no thread support either.

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 18790
    MAX_BODY_BYTES = 1_048_576  # 1 MB

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._host = str(config.get("host", self.DEFAULT_HOST))
        self._port = int(config.get("port", self.DEFAULT_PORT))
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        # Per-token rate-limit window: {token_id: [timestamp, ...]}
        self._rate_window: dict[str, list[float]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        self._app = web.Application(client_max_size=self.MAX_BODY_BYTES)
        self._app.router.add_post("/webhook/{token_id}", self._handle_webhook)
        self._app.router.add_get("/webhook/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await self._site.start()
        except OSError as exc:
            logger.error("webhook bind failed on %s:%d: %s", self._host, self._port, exc)
            return False
        logger.info("webhook: listening on http://%s:%d/webhook/<token_id>", self._host, self._port)
        return True

    async def disconnect(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("webhook site stop: %s", exc)
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception as exc:  # noqa: BLE001
                logger.warning("webhook runner cleanup: %s", exc)

    # ------------------------------------------------------------------
    # Outbound — webhooks are inbound-only
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Webhooks are one-shot triggers, not chats. ``send`` is unsupported.

        If the agent wants to deliver a result, it must be done through
        another channel (telegram/discord) or via the optional
        ``reply_url`` field in the inbound payload (TODO follow-up).
        """
        return SendResult(
            success=False,
            error="webhook adapter is inbound-only; use a different channel for outbound",
        )

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Liveness check. No auth, no payload."""
        return web.json_response({"ok": True, "service": "opencomputer-webhook"})

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        token_id = request.match_info["token_id"]

        # Rate limit BEFORE any DB lookup to make hammering cheap to reject.
        if not self._check_rate_limit(token_id):
            return web.json_response(
                {"error": "rate limited"},
                status=429,
            )

        token_meta = get_token(token_id)
        if not token_meta or token_meta.get("revoked"):
            return web.json_response({"error": "unknown or revoked token"}, status=401)

        body = await request.read()
        signature = request.headers.get("X-Webhook-Signature", "")
        if not verify_signature(body=body, signature_header=signature, secret=token_meta["secret"]):
            return web.json_response({"error": "invalid signature"}, status=403)

        # Parse payload — accept JSON or text/plain.
        try:
            if request.content_type == "application/json":
                payload = await request.json()
            else:
                payload = {"text": body.decode("utf-8", errors="replace")}
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"malformed body: {exc}"}, status=400)

        # Build MessageEvent for dispatch.
        text = _coerce_text(payload)
        if not text:
            return web.json_response(
                {"error": "payload must include 'text' string or be a string body"},
                status=400,
            )

        event = MessageEvent(
            platform=Platform.WEB,
            chat_id=f"webhook:{token_id}",
            user_id=f"webhook:{token_meta.get('name', token_id[:8])}",
            text=text,
            timestamp=time.time(),
            metadata={
                "webhook_token_id": token_id,
                "webhook_token_name": token_meta.get("name"),
                "webhook_scopes": token_meta.get("scopes", []),
                "webhook_notify": token_meta.get("notify"),
                "payload": payload if isinstance(payload, dict) else None,
            },
        )

        mark_used(token_id)

        # Dispatch and wait for the agent's response (so the webhook caller
        # gets a meaningful return value, not just an ack).
        try:
            await self.handle_message(event)
        except Exception as exc:  # noqa: BLE001
            logger.exception("webhook dispatch failed for token=%s", token_id)
            return web.json_response(
                {"error": f"dispatch failed: {type(exc).__name__}: {exc}"},
                status=500,
            )

        return web.json_response({"ok": True, "received_at": event.timestamp})

    # ------------------------------------------------------------------
    # Rate-limit helper (per-token sliding window)
    # ------------------------------------------------------------------

    def _check_rate_limit(self, token_id: str) -> bool:
        now = time.monotonic()
        window = self._rate_window[token_id]
        # Drop timestamps older than the window
        cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.pop(0)
        if len(window) >= _RATE_LIMIT_REQS:
            return False
        window.append(now)
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_text(payload: Any) -> str:
    """Pull a sensible ``text`` field out of an arbitrary webhook payload.

    Common shapes we accept:
    - ``{"text": "..."}`` (TradingView, custom)
    - ``{"alert": "..."}`` → mapped to text
    - ``{"message": "..."}`` (generic)
    - ``{"event": "...", "ticker": "..."}`` → flatten to "<event> <ticker>"
    - Plain string body
    """
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return str(payload)

    for key in ("text", "alert", "message", "body", "content"):
        if (val := payload.get(key)) and isinstance(val, str):
            return val.strip()

    # Some webhooks send structured event-like payloads; flatten top-level
    # string fields into one line so the agent has something to work with.
    parts: list[str] = []
    for k, v in payload.items():
        if isinstance(v, str | int | float | bool):
            parts.append(f"{k}={v}")
    return " ".join(parts).strip()


__all__ = ["WebhookAdapter"]
