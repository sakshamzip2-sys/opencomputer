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
import re
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
        load_tokens,
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
    load_tokens = _tokens_mod.load_tokens
    mark_used = _tokens_mod.mark_used
    verify_signature = _tokens_mod.verify_signature

logger = logging.getLogger("opencomputer.ext.webhook")


# Per-token rate limit. 60 POSTs/min/token is generous for normal triggers
# (TradingView alerts fire ≤1/min) but blocks runaway clients.
_RATE_LIMIT_REQS = 60
_RATE_LIMIT_WINDOW_SECONDS = 60.0


class WebhookAdapter(BaseChannelAdapter):
    platform = Platform.WEBHOOK
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
        # Hermes channel-port PR 3c.5: handle to the PluginAPI so we can
        # reach ``api.outgoing_queue`` at delivery time. Set by the
        # plugin's ``register(api)`` via ``bind_plugin_api(api)`` —
        # ``None`` outside the gateway (CLI / tests / direct calls). The
        # deliver-only path falls back to logging + an HTTP 503 when
        # this is ``None`` so misconfigured deployments fail loudly.
        self._plugin_api: Any = None

    def bind_plugin_api(self, api: Any) -> None:
        """Late-bind a PluginAPI handle.

        Called from the plugin's ``register(api)`` after the adapter is
        constructed. The adapter stashes the reference so the
        deliver-only request handler can reach
        ``api.outgoing_queue.enqueue(...)`` without re-importing the
        gateway internals.
        """
        self._plugin_api = api

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        # Validate every deliver_only token against the set of registered
        # adapter platforms BEFORE binding the listener. Unmatched
        # delivery_target values would otherwise produce "queued but
        # never sent" messages that pile up silently — far worse than
        # refusing to start.
        if not self._validate_deliver_only_tokens():
            return False

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

    def _validate_deliver_only_tokens(self) -> bool:
        """Check every deliver_only token's ``delivery_target.platform``
        names a registered adapter. Returns True when every token is
        either standard (no deliver_only) or pointed at a known
        platform. Logs ERROR + returns False if any deliver_only token
        is misconfigured — caller treats False as "refuse to start".

        Tolerant of the "no PluginAPI bound" case (CLI / tests): the
        validator is a no-op when there's no api handle.
        """
        if self._plugin_api is None:
            return True  # CLI / tests — nothing to validate against
        try:
            tokens = load_tokens()
        except Exception as exc:  # noqa: BLE001
            logger.warning("webhook: token registry unreadable, skipping validation: %s", exc)
            return True
        # Pull the set of registered channel-platform names. Per
        # PluginRegistry contract `api.channels` keys are platform
        # *string* values (Platform.X.value).
        known_platforms = set(getattr(self._plugin_api, "channels", {}) or {})
        ok = True
        for tid, meta in tokens.items():
            if not meta.get("deliver_only"):
                continue
            target = meta.get("delivery_target") or {}
            platform = (target or {}).get("platform")
            chat_id = (target or {}).get("chat_id")
            if not platform or not chat_id:
                logger.error(
                    "webhook: token %s is deliver_only but delivery_target is "
                    "missing platform or chat_id: %r",
                    tid, target,
                )
                ok = False
                continue
            if platform not in known_platforms:
                logger.error(
                    "webhook: token %s deliver_only target platform %r is "
                    "not a registered adapter (registered: %s)",
                    tid, platform, sorted(known_platforms),
                )
                ok = False
        return ok

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

        # ── Deliver-only mode ──────────────────────────────────────
        # When a token is configured with ``deliver_only: true``, the
        # webhook does NOT run the agent. Instead it renders an optional
        # template against the payload and enqueues the result on the
        # outgoing-queue facade for delivery to ``delivery_target``.
        # Use case: cron-like external services (UptimeRobot, GitHub
        # Actions, TradingView "send-only" alerts) that already produce
        # the final user-facing string.
        if token_meta.get("deliver_only"):
            return await self._handle_deliver_only(
                token_id=token_id, token_meta=token_meta, payload=payload
            )

        # Build MessageEvent for dispatch.
        text = _coerce_text(payload)
        if not text:
            return web.json_response(
                {"error": "payload must include 'text' string or be a string body"},
                status=400,
            )

        event = MessageEvent(
            platform=Platform.WEBHOOK,
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

    async def _handle_deliver_only(
        self,
        *,
        token_id: str,
        token_meta: dict[str, Any],
        payload: Any,
    ) -> web.Response:
        """Deliver-only branch: render template + enqueue, no agent run."""
        target = token_meta.get("delivery_target") or {}
        platform = target.get("platform")
        chat_id = target.get("chat_id")
        if not platform or not chat_id:
            logger.error(
                "webhook: token=%s deliver_only is set but delivery_target is "
                "incomplete: %r",
                token_id, target,
            )
            return web.json_response(
                {"error": "deliver_only token misconfigured (delivery_target)"},
                status=500,
            )

        template = token_meta.get("template") or ""
        body = _render_prompt(template, payload) if template else _coerce_text(payload)
        if not body:
            return web.json_response(
                {"error": "rendered body is empty (payload + template produced no text)"},
                status=400,
            )

        api = self._plugin_api
        queue = getattr(api, "outgoing_queue", None) if api is not None else None
        if queue is None:
            logger.error(
                "webhook deliver_only: no outgoing_queue bound (token=%s); dropping",
                token_id,
            )
            return web.json_response(
                {"error": "outgoing_queue unavailable"},
                status=503,
            )

        try:
            result = queue.enqueue(
                platform=str(platform),
                chat_id=str(chat_id),
                body=body,
                metadata={
                    "source": "webhook_deliver_only",
                    "webhook_token_id": token_id,
                    "webhook_token_name": token_meta.get("name"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("webhook deliver_only enqueue failed token=%s", token_id)
            return web.json_response(
                {"error": f"enqueue failed: {type(exc).__name__}: {exc}"},
                status=500,
            )

        # ``result`` is duck-typed; tests pass a Mock. Don't crash if
        # the queue returns ``None`` (best-effort enqueue stubs).
        msg_id = getattr(result, "id", None) if result is not None else None
        mark_used(token_id)
        return web.json_response(
            {
                "ok": True,
                "delivered": False,  # truth is async — caller knows it's queued
                "queued": True,
                "platform": platform,
                "chat_id": chat_id,
                "queue_id": msg_id,
            }
        )

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


# ``{{key}}`` substitution. Whitespace inside the braces is allowed and
# trimmed (mimics minimal-Jinja). Missing keys render as empty string —
# choosing empty over an error keeps deliver_only resilient to optional
# fields in the source webhook (TradingView "alert.message" sometimes
# absent, etc.). Nested keys not supported on purpose; this is "render a
# notification line", not a templating engine.
_PROMPT_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _render_prompt(template: str, payload: Any) -> str:
    """Render ``{{key}}`` placeholders in ``template`` against ``payload``.

    - ``template`` empty → return ``""``.
    - ``payload`` not a dict → only ``{{value}}`` resolves (full payload).
    - Missing key → empty string substitution.
    - All values are stringified via ``str(...)`` to be safe.

    No HTML escaping (the rendered string flows to a chat platform, not
    a browser). No expression evaluation (security: never eval user
    input).
    """
    if not template:
        return ""
    data = payload if isinstance(payload, dict) else {"value": payload}

    def _replace(m: re.Match[str]) -> str:
        key = m.group(1)
        val = data.get(key, "")
        return "" if val is None else str(val)

    return _PROMPT_VAR_RE.sub(_replace, template)


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


__all__ = ["WebhookAdapter", "_render_prompt"]
