"""APIServerAdapter — REST endpoint exposing the agent over HTTP (G.28 / Tier 4.x).

Differs from the other Tier 4 adapters: it doesn't connect TO an
external service — it EXPOSES an HTTP server callers POST to, like
``opencomputer wire`` but over plain JSON-over-HTTP rather than
WebSocket.

Endpoint shape::

    POST /v1/chat
    Authorization: Bearer <token>
    Content-Type: application/json

    {"session_id": "<optional>", "message": "<user text>"}

Response::

    {"session_id": "<id>", "response": "<agent reply>"}

Currently the adapter is a thin ``aiohttp`` server that only exposes
the endpoint contract — wiring it into the actual agent loop happens
when the host calls ``set_handler(callable)`` after registration. This
keeps the SDK boundary clean: the adapter doesn't import from
``opencomputer.*``, the host injects the handler.

Bind defaults to ``127.0.0.1`` so a misconfigured install doesn't
expose the agent to the public internet. To bind publicly the user
must explicitly set ``API_SERVER_HOST=0.0.0.0`` AND set a strong
``API_SERVER_TOKEN``.

Capabilities: none of the message-shaping flags apply — this is a
request/response surface, not a streaming chat channel.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, SendResult

logger = logging.getLogger("opencomputer.ext.api_server")


# Type alias for the handler the host injects. Takes (session_id, text)
# and returns the agent's reply.
ChatHandler = Callable[[str, str], Awaitable[str]]


class APIServerAdapter(BaseChannelAdapter):
    """REST API channel — exposes /v1/chat for external callers."""

    platform = Platform.WEB
    max_message_length = 100_000
    """Generous cap — REST callers may legitimately POST larger payloads
    than chat platforms (e.g. a CI-system POSTing a build log). Still
    bounded so a misbehaving caller can't OOM the process."""

    capabilities = ChannelCapabilities(0)
    """No message-shaping capabilities — request/response surface only."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._host: str = config.get("host", "127.0.0.1")
        self._port: int = int(config.get("port", 18791))
        self._token: str = config["token"]
        self._handler: ChatHandler | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def set_handler(self, handler: ChatHandler) -> None:
        """Inject the per-request agent handler.

        The host (``opencomputer.gateway`` or a custom embed) calls this
        after registration. Without a handler set, requests return 503.
        """
        self._handler = handler

    # ─── HTTP handler ───────────────────────────────────────────────

    async def _handle_chat(self, request: web.Request) -> web.Response:
        # Auth: Bearer token must match the configured value exactly.
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != self._token:
            return web.json_response(
                {"error": "unauthorized"}, status=401
            )
        if request.content_length and request.content_length > self.max_message_length:
            return web.json_response(
                {"error": "payload too large"}, status=413
            )
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response(
                {"error": "invalid json body"}, status=400
            )
        message = payload.get("message", "")
        session_id = payload.get("session_id", "")
        if not isinstance(message, str) or not message.strip():
            return web.json_response(
                {"error": "missing or empty 'message' field"}, status=400
            )
        if self._handler is None:
            return web.json_response(
                {"error": "agent handler not bound"}, status=503
            )
        try:
            reply = await self._handler(session_id, message)
        except Exception as e:  # noqa: BLE001
            logger.exception("api-server handler raised")
            return web.json_response(
                {"error": f"handler error: {type(e).__name__}"}, status=500
            )
        return web.json_response(
            {"session_id": session_id, "response": reply}
        )

    # ─── Server lifecycle ───────────────────────────────────────────

    def _build_app(self) -> web.Application:
        # Limit per-request body size at the framework level so large
        # uploads don't even reach the handler.
        app = web.Application(client_max_size=self.max_message_length)
        app.router.add_post("/v1/chat", self._handle_chat)
        return app

    async def connect(self) -> None:
        """Start the aiohttp server bound to host:port."""
        if self._runner is not None:
            return
        app = self._build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info(
            "api-server listening on http://%s:%d/v1/chat", self._host, self._port
        )

    async def disconnect(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ─── Outbound: not applicable ───────────────────────────────────

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        # API server is request/response — there's no "outbound" send
        # outside of the response to an active request. Return a clear
        # not-implemented so any caller that mistakenly tries to use
        # this adapter as a chat channel sees a useful error.
        return SendResult(
            success=False,
            error=(
                "api-server is a REST endpoint, not a push channel — "
                "callers receive responses synchronously via POST /v1/chat"
            ),
        )
