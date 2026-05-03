"""CSRF middleware — loopback-only mutation guard.

Defends against malicious local websites making fetch() calls to the
loopback API. Mirrors OpenClaw's csrf.ts:27-56 precedence:

  1. method not in {POST, PUT, PATCH, DELETE} → bypass.
  2. OPTIONS preflight → bypass.
  3. ``Sec-Fetch-Site: cross-site`` → reject.
  4. ``Origin`` non-empty → must be loopback else reject.
  5. ``Referer`` non-empty (and Origin empty) → must be loopback else reject.
  6. Neither header present → pass (likely curl/Node; auth still gates).

``Origin: null`` is treated as not-loopback (sandboxed iframes etc.).
``Sec-Fetch-Site`` values other than ``cross-site`` (``same-origin``,
``same-site``, ``none``) do NOT short-circuit — they fall through to
Origin/Referer because ``localhost`` and ``127.0.0.1`` produce
``same-site`` even though the API only accepts loopback.
"""

from __future__ import annotations

from collections.abc import Mapping
from ipaddress import ip_address
from urllib.parse import urlparse

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_loopback_url(value: str | None) -> bool:
    if not value:
        return False
    s = value.strip()
    if not s or s == "null":
        return False
    try:
        host = (urlparse(s).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in _LOOPBACK_HOSTNAMES:
        return True
    if host.startswith("127."):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def should_reject_browser_mutation(
    *,
    method: str,
    origin: str | None = None,
    referer: str | None = None,
    sec_fetch_site: str | None = None,
) -> bool:
    """Return True if the request should be 403'd."""
    if method.upper() not in _MUTATING_METHODS:
        return False
    if (sec_fetch_site or "").lower() == "cross-site":
        return True
    o = (origin or "").strip()
    if o:
        return not is_loopback_url(o)
    r = (referer or "").strip()
    if r:
        return not is_loopback_url(r)
    return False


# ─── ASGI middleware ─────────────────────────────────────────────────


class CSRFMiddleware:
    """Starlette/FastAPI-compatible ASGI middleware.

    Add as the *outermost* middleware so we 403 before reaching auth
    (a non-loopback unauthenticated request should hit 403, not 401).
    """

    def __init__(self, app: object) -> None:
        self.app = app

    async def __call__(self, scope: Mapping[str, object], receive: object, send: object) -> object:  # type: ignore[override]
        if scope["type"] != "http":
            return await self.app(scope, receive, send)  # type: ignore[misc]
        method = str(scope.get("method", "GET")).upper()
        if method == "OPTIONS":
            return await self.app(scope, receive, send)  # type: ignore[misc]
        if method not in _MUTATING_METHODS:
            return await self.app(scope, receive, send)  # type: ignore[misc]

        headers = self._headers_dict(scope.get("headers") or [])  # type: ignore[arg-type]
        if should_reject_browser_mutation(
            method=method,
            origin=headers.get("origin"),
            referer=headers.get("referer"),
            sec_fetch_site=headers.get("sec-fetch-site"),
        ):
            await self._send_403(send)  # type: ignore[arg-type]
            return None
        return await self.app(scope, receive, send)  # type: ignore[misc]

    @staticmethod
    def _headers_dict(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in headers:
            try:
                out[k.decode("latin-1").lower()] = v.decode("latin-1")
            except Exception:
                continue
        return out

    @staticmethod
    async def _send_403(send: object) -> None:
        await send(  # type: ignore[misc]
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"Forbidden"})  # type: ignore[misc]
