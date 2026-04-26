"""Browser-bridge adapter — aiohttp endpoint receiving Chrome-extension POSTs.

Exposes ``POST /browser-event`` on a configurable port (default 18791).
Bearer-token auth (token regenerated per profile install). Validates
payload shape, then publishes a :class:`plugin_sdk.ingestion.SignalEvent`
with ``event_type="browser_visit"`` to the in-process bus.

Cross-origin: allows extensions to POST from any origin — the bearer
token is the auth gate.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from aiohttp import web

from opencomputer.ingestion.bus import TypedEventBus
from plugin_sdk.ingestion import SignalEvent

_log = logging.getLogger("extensions.browser_bridge")


def generate_token() -> str:
    """Generate a 32-byte URL-safe token for browser-extension auth."""
    return secrets.token_urlsafe(32)


class BrowserBridgeAdapter:
    """HTTP listener bound to localhost. Publishes events into a TypedEventBus.

    The :meth:`start` method propagates :class:`OSError` raised by
    ``aiohttp`` when the port is already bound — callers should surface
    this with an actionable message ("port 18791 in use; try
    `lsof -ti:18791 | xargs kill -9`").
    """

    def __init__(
        self,
        *,
        bus: TypedEventBus,
        port: int = 18791,
        token: str = "",
        bind: str = "127.0.0.1",
    ) -> None:
        self._bus = bus
        self._port = port
        self._token = token
        self._bind = bind
        self._runner: web.AppRunner | None = None

    async def start(self) -> web.AppRunner:
        app = web.Application(client_max_size=512 * 1024)  # 512KB cap
        app.router.add_post("/browser-event", self._handle)
        app.router.add_get("/health", self._health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._bind, self._port)
        await site.start()  # raises OSError on EADDRINUSE — let it bubble
        self._runner = runner
        _log.info("browser-bridge listening on %s:%s", self._bind, self._port)
        return runner

    async def stop(self) -> None:
        """Tear down the listener. Idempotent — safe to call when not started.

        Wraps :meth:`aiohttp.web.AppRunner.cleanup` so callers (the CLI
        ``bridge start`` foreground loop, future supervisors, tests) have
        a stable shutdown verb that doesn't reach into the aiohttp
        internals. After ``stop`` the adapter can be discarded; ``start``
        on the same instance is not supported and would re-bind the port.
        """
        if self._runner is None:
            return
        runner, self._runner = self._runner, None
        await runner.cleanup()

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle(self, request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {self._token}"
        if self._token and auth != expected:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload: dict[str, Any] = await request.json()
        except (web.HTTPBadRequest, ValueError):
            return web.json_response({"error": "bad_json"}, status=400)
        url = payload.get("url")
        if not isinstance(url, str) or not url:
            return web.json_response({"error": "missing url"}, status=400)
        title = str(payload.get("title", ""))[:256]
        visit_time = float(payload.get("visit_time") or time.time())
        event = SignalEvent(
            event_type="browser_visit",
            source="browser-bridge",
            timestamp=visit_time,
            metadata={"url": url[:2048], "title": title},
        )
        self._bus.publish(event)
        return web.json_response({"status": "ok"})
