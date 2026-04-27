"""Telegram webhook receiver + setup helpers (Round 4 Item 3).

Webhook mode is strictly better than polling — saves bandwidth, no
30-second poll latency, no dropped updates on connection blips. The
catch is needing a public HTTPS endpoint. We support two paths:

1. **VPS deployment** — user runs OC behind a reverse proxy with TLS.
2. **Tunnel on Mac** — user runs ngrok or cloudflared; we detect it.

Hermes uses ``python-telegram-bot``'s built-in
``Application.updater.start_webhook()``. We use raw aiohttp here to
match OC's existing httpx-only HTTP posture (no new heavy deps).

Telegram security: the ``setWebhook`` API accepts a ``secret_token``
which Telegram echoes back in the ``X-Telegram-Bot-Api-Secret-Token``
header on every push. We verify it constant-time on receive.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from aiohttp import web

logger = logging.getLogger(__name__)


def generate_secret_token() -> str:
    """Generate a Telegram-compatible secret_token.

    Telegram constraints: 1-256 chars, only ``A-Za-z0-9_-``. ~32 bytes
    of entropy is overkill for a per-bot endpoint; 256 bits gives us
    plenty of room.
    """
    return secrets.token_urlsafe(32)


async def set_webhook(
    *,
    token: str,
    url: str,
    secret_token: str,
    drop_pending: bool = False,
    allowed_updates: list[str] | None = None,
) -> tuple[bool, str]:
    """Register the webhook URL with Telegram.

    Returns ``(ok, message)``. ``message`` is the API's ``description``
    field on success, or the error reason on failure.

    ``drop_pending=True`` clears Telegram's queue of polled-but-not-
    delivered updates. Useful when switching from polling → webhook so
    we don't re-process the backlog.
    """
    body: dict[str, Any] = {
        "url": url,
        "secret_token": secret_token,
    }
    if drop_pending:
        body["drop_pending_updates"] = True
    if allowed_updates is not None:
        body["allowed_updates"] = allowed_updates

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/setWebhook", json=body
            )
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        return False, f"setWebhook HTTP failure: {exc}"

    if not data.get("ok"):
        return False, data.get("description", "unknown error")
    return True, data.get("description", "Webhook was set")


async def delete_webhook(*, token: str) -> tuple[bool, str]:
    """Tear down a previously-registered webhook.

    Idempotent — Telegram returns ``ok: true`` even if no webhook was
    set. Use this before switching back to polling, otherwise polling
    fights with the (still-registered) webhook.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/deleteWebhook"
            )
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        return False, f"deleteWebhook HTTP failure: {exc}"

    if not data.get("ok"):
        return False, data.get("description", "unknown error")
    return True, data.get("description", "Webhook deleted")


async def get_webhook_info(*, token: str) -> dict[str, Any]:
    """Fetch Telegram's view of the registered webhook.

    Returns the ``result`` dict on success, ``{}`` on failure. Useful
    for ``opencomputer telegram webhook status`` to confirm OUR URL is
    set (vs. an unrelated process having set its own).
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/getWebhookInfo"
            )
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("getWebhookInfo failed: %s", exc)
        return {}
    return data.get("result", {}) if data.get("ok") else {}


def _verify_secret_header(request: web.Request, expected: str) -> bool:
    """Constant-time compare of the X-Telegram-Bot-Api-Secret-Token header.

    Returns False on missing/wrong header. Telegram only sends the
    header on requests from its servers; missing header = forged
    request, drop it.
    """
    actual = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8"))


async def start_webhook_server(
    *,
    secret_token: str,
    port: int,
    handle_update: Callable[[dict[str, Any]], Awaitable[None]],
    listen: str = "0.0.0.0",
    path: str = "/telegram/webhook",
) -> web.AppRunner:
    """Start an aiohttp server that receives Telegram update POSTs.

    Returns the ``AppRunner`` so the caller can ``await runner.cleanup()``
    on shutdown. The server runs the ``handle_update`` coroutine for
    every authenticated request. Bad-secret requests are 403'd; bad-JSON
    requests are 400'd; ``handle_update`` exceptions are caught and
    logged so a single bad update doesn't tear down the server.
    """
    async def _on_post(request: web.Request) -> web.Response:
        if not _verify_secret_header(request, secret_token):
            logger.warning(
                "telegram webhook: bad/missing secret token from %s",
                request.remote,
            )
            return web.Response(status=403, text="forbidden")
        try:
            update = await request.json()
        except Exception:  # noqa: BLE001 — bad JSON from a forged client
            return web.Response(status=400, text="bad json")
        try:
            await handle_update(update)
        except Exception as exc:  # noqa: BLE001 — must not kill the server
            logger.exception("telegram webhook: handle_update failed: %s", exc)
        # Telegram retries on non-2xx responses. We always return 200
        # so a bad update isn't redelivered forever; the exception was
        # logged for debugging.
        return web.Response(status=200, text="ok")

    app = web.Application()
    app.router.add_post(path, _on_post)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, listen, port)
    await site.start()
    logger.info("telegram webhook server listening on %s:%d%s", listen, port, path)
    return runner


# ─── Tunnel detection (macOS UX) ────────────────────────────────────────


async def detect_ngrok_url() -> str | None:
    """Probe ngrok's local API (default port 4040) for an HTTPS tunnel.

    Returns the public ``https://…ngrok.io`` URL or ``None`` when
    ngrok isn't running. Used by ``opencomputer telegram tunnel detect``
    to auto-fill the webhook URL on a Mac.
    """
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get("http://127.0.0.1:4040/api/tunnels")
            data = r.json()
    except Exception:  # noqa: BLE001 — ngrok not running, that's fine
        return None
    for tunnel in data.get("tunnels", []):
        url = tunnel.get("public_url", "")
        if url.startswith("https://"):
            return url
    return None


def detect_cloudflared_running() -> bool:
    """Cheap check for a cloudflared tunnel process.

    Cloudflared doesn't expose a local API like ngrok does, so we just
    confirm the process is running. Caller still needs the public URL
    from the user's tunnel config — we surface "found cloudflared,
    can't auto-detect URL" as the error.
    """
    import subprocess

    try:
        r = subprocess.run(
            ["pgrep", "-x", "cloudflared"],
            capture_output=True,
            timeout=2,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


__all__ = [
    "delete_webhook",
    "detect_cloudflared_running",
    "detect_ngrok_url",
    "generate_secret_token",
    "get_webhook_info",
    "set_webhook",
    "start_webhook_server",
]
