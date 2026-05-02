"""WebhookInboundAdapter — multi-platform inbound HTTP listener.

Single aiohttp server with three platform-specific routes:

  POST /inbound/teams/{token_id}     → Teams Outgoing Webhook
  POST /inbound/dingtalk/{token_id}  → DingTalk Outgoing Bot
  POST /inbound/feishu/{token_id}    → Feishu Custom Bot Callback

Each path verifies the platform's native signature scheme using a per-token
secret stored in the same registry the generic ``webhook`` plugin uses
(``<profile_home>/webhook_tokens.json``). Verified messages are dispatched
to the gateway as :class:`MessageEvent`.

Security:

  - Constant-time HMAC compare (verifiers.py uses ``hmac.compare_digest``).
  - 1 MB body cap (``client_max_size``) to prevent OOM.
  - Per-token rate limit (60 req/min) shared across all platform paths.

Outbound is intentionally unsupported here — outbound is what the existing
``teams`` / ``dingtalk`` / ``feishu`` plugins already do.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path as _Path
from typing import Any

from aiohttp import web

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import MessageEvent, Platform, SendResult


# Plugin-loader mode (sys.path includes plugin dir): plain import.
# Test/package mode: load sibling files explicitly.
def _load_local_module(name: str) -> Any:
    """Load a sibling .py module under a unique sys.modules key.

    ``importlib.util.spec_from_file_location`` with a synthetic name avoids
    collisions with other plugins that also have ``verifiers.py``.
    """
    here = _Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        f"_webhook_inbound_{name}", here / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_verifiers = _load_local_module("verifiers")
verify_teams = _verifiers.verify_teams
verify_dingtalk = _verifiers.verify_dingtalk
verify_feishu = _verifiers.verify_feishu
extract_teams_message = _verifiers.extract_teams_message
extract_dingtalk_message = _verifiers.extract_dingtalk_message
extract_feishu_message = _verifiers.extract_feishu_message
extract_feishu_challenge = _verifiers.extract_feishu_challenge


# Token store — reuse the generic webhook plugin's token JSON file.
_WEBHOOK_TOKENS_PY = (
    _Path(__file__).resolve().parent.parent / "webhook" / "tokens.py"
)


def _load_tokens_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_webhook_inbound_tokens", _WEBHOOK_TOKENS_PY
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_tokens_mod = _load_tokens_module()
get_token = _tokens_mod.get_token
mark_used = _tokens_mod.mark_used


logger = logging.getLogger("opencomputer.ext.webhook_inbound")

_RATE_LIMIT_REQS = 60
_RATE_LIMIT_WINDOW_SECONDS = 60.0


class WebhookInboundAdapter(BaseChannelAdapter):
    """Multi-platform inbound webhook listener (Teams / DingTalk / Feishu).

    Outbound delivery for these platforms lives in the per-platform plugins
    (``extensions/teams/``, ``extensions/dingtalk/``, ``extensions/feishu/``).
    This adapter is inbound-only.
    """

    platform = Platform.WEBHOOK
    max_message_length = 64_000
    capabilities = ChannelCapabilities.NONE

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 18791  # +1 from the generic webhook plugin (18790)
    MAX_BODY_BYTES = 1_048_576  # 1 MB

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._host = str(config.get("host", self.DEFAULT_HOST))
        self._port = int(config.get("port", self.DEFAULT_PORT))
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._rate_window: dict[str, list[float]] = defaultdict(list)
        self._plugin_api: Any = None

    def bind_plugin_api(self, api: Any) -> None:
        """Receive the PluginAPI handle (for outgoing-queue dispatch)."""
        self._plugin_api = api

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def connect(self) -> bool:
        self._app = web.Application(client_max_size=self.MAX_BODY_BYTES)
        self._app.router.add_post("/inbound/teams/{token_id}", self._handle_teams)
        self._app.router.add_post(
            "/inbound/dingtalk/{token_id}", self._handle_dingtalk
        )
        self._app.router.add_post(
            "/inbound/feishu/{token_id}", self._handle_feishu
        )
        self._app.router.add_get("/inbound/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await self._site.start()
        except OSError as exc:
            logger.error(
                "webhook-inbound bind failed on %s:%d: %s",
                self._host, self._port, exc,
            )
            return False
        logger.info(
            "webhook-inbound: listening on http://%s:%d/inbound/{platform}/{token_id}",
            self._host, self._port,
        )
        return True

    async def disconnect(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("webhook-inbound site stop: %s", exc)
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception as exc:  # noqa: BLE001
                logger.warning("webhook-inbound runner cleanup: %s", exc)

    # ------------------------------------------------------------------ #
    # Outbound — unsupported
    # ------------------------------------------------------------------ #

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        return SendResult(
            success=False,
            error=(
                "webhook-inbound is inbound-only — use the per-platform "
                "outbound plugins (teams / dingtalk / feishu) for sending"
            ),
        )

    # ------------------------------------------------------------------ #
    # Rate limiting
    # ------------------------------------------------------------------ #

    def _is_rate_limited(self, token_id: str) -> bool:
        """Return True (and prune the window) if this token exceeded its quota."""
        now = time.time()
        window = self._rate_window[token_id]
        cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
        # Prune in place
        i = 0
        while i < len(window) and window[i] < cutoff:
            i += 1
        if i:
            del window[:i]
        if len(window) >= _RATE_LIMIT_REQS:
            return True
        window.append(now)
        return False

    # ------------------------------------------------------------------ #
    # Request handlers
    # ------------------------------------------------------------------ #

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _resolve_token(self, request: web.Request) -> tuple[str, dict[str, Any]] | None:
        """Common token-lookup path. Returns (token_id, meta) or None+401."""
        token_id = request.match_info.get("token_id", "")
        meta = get_token(token_id) if token_id else None
        if not meta or meta.get("revoked"):
            return None
        if self._is_rate_limited(token_id):
            return None
        return token_id, meta

    async def _handle_teams(self, request: web.Request) -> web.Response:
        body = await request.read()
        resolved = await self._resolve_token(request)
        if resolved is None:
            return web.Response(status=401, text="invalid token")
        token_id, meta = resolved
        secret = str(meta.get("secret") or "")

        auth = request.headers.get("Authorization", "")
        if not verify_teams(auth, body, secret):
            return web.Response(status=401, text="bad signature")

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid json")

        msg = extract_teams_message(payload if isinstance(payload, dict) else {})
        await self._dispatch(msg, token_id, "teams")
        mark_used(token_id)
        return web.Response(text="ok")

    async def _handle_dingtalk(self, request: web.Request) -> web.Response:
        body = await request.read()
        resolved = await self._resolve_token(request)
        if resolved is None:
            return web.Response(status=401, text="invalid token")
        token_id, meta = resolved
        secret = str(meta.get("secret") or "")

        timestamp = request.headers.get("timestamp", "")
        sign = request.headers.get("sign", "")
        if not verify_dingtalk(timestamp, sign, secret):
            return web.Response(status=401, text="bad signature")

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid json")

        msg = extract_dingtalk_message(payload if isinstance(payload, dict) else {})
        await self._dispatch(msg, token_id, "dingtalk")
        mark_used(token_id)
        return web.Response(text="ok")

    async def _handle_feishu(self, request: web.Request) -> web.Response:
        body = await request.read()
        resolved = await self._resolve_token(request)
        if resolved is None:
            return web.Response(status=401, text="invalid token")
        token_id, meta = resolved
        secret = str(meta.get("secret") or "")

        # URL-verification challenge — Feishu sends it on first activation.
        # No signature header on this initial probe, so verify only after
        # rejecting non-handshake unsigned bodies.
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid json")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="invalid json")

        challenge = extract_feishu_challenge(payload)
        if challenge is not None:
            # Echo the challenge per Feishu's URL-verification protocol
            return web.json_response({"challenge": challenge})

        timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
        sign = request.headers.get("X-Lark-Signature", "")
        if not verify_feishu(timestamp, sign, secret):
            return web.Response(status=401, text="bad signature")

        msg = extract_feishu_message(payload)
        await self._dispatch(msg, token_id, "feishu")
        mark_used(token_id)
        return web.Response(text="ok")

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    async def _dispatch(
        self, msg: Any, token_id: str, platform: str
    ) -> None:
        """Hand the verified message to the gateway."""
        if not msg.text:
            return  # Empty / unsupported message types are silently dropped
        if self._plugin_api is None:
            logger.warning(
                "webhook-inbound: PluginAPI not bound; dropping message from %s",
                platform,
            )
            return
        event = MessageEvent(
            platform=Platform.WEBHOOK,
            chat_id=msg.chat_id or token_id,
            user_id=msg.sender_id or "",
            text=msg.text,
            timestamp=time.time(),
            metadata={
                "inbound_platform": platform,
                "token_id": token_id,
                "sender_name": msg.sender_name,
            },
        )
        dispatch = getattr(self._plugin_api, "dispatch_message", None)
        if callable(dispatch):
            await dispatch(event)
