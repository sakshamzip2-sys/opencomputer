"""Auth middleware + body-limit middleware.

Middleware ordering (outermost → innermost) on the FastAPI app:

  CSRFMiddleware     →  loopback-only mutation guard (returns 403)
  BrowserAuthMiddleware →  Bearer/X-OpenComputer-Password (returns 401)
  BodyLimitMiddleware →  413 if body exceeds 1MB

In FastAPI, ``add_middleware`` calls produce **reverse** order on
incoming requests (last added = innermost). ``app.py`` adds them in the
right order so that:

  - Cross-site POST → CSRF 403 (never reaches auth).
  - Loopback no-creds → auth 401.
  - Loopback authed huge body → body-limit 413.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .auth import BrowserAuth, is_authorized

DEFAULT_BODY_LIMIT_BYTES = 1_000_000  # OpenClaw uses 1MB


class BrowserAuthMiddleware:
    def __init__(self, app: Any, auth: BrowserAuth) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Mapping[str, object], receive: object, send: object) -> object:  # type: ignore[override]
        if scope["type"] != "http":
            return await self.app(scope, receive, send)  # type: ignore[misc]
        if self.auth.is_anonymous_allowed():
            return await self.app(scope, receive, send)  # type: ignore[misc]

        headers = _headers_dict(scope.get("headers") or [])  # type: ignore[arg-type]
        if is_authorized(headers, self.auth):
            return await self.app(scope, receive, send)  # type: ignore[misc]

        await _send_status(send, 401, b'{"error":"unauthorized"}', "application/json")  # type: ignore[arg-type]
        return None


class BodyLimitMiddleware:
    def __init__(self, app: Any, limit_bytes: int = DEFAULT_BODY_LIMIT_BYTES) -> None:
        self.app = app
        self.limit_bytes = int(limit_bytes)

    async def __call__(self, scope: Mapping[str, object], receive: object, send: object) -> object:  # type: ignore[override]
        if scope["type"] != "http":
            return await self.app(scope, receive, send)  # type: ignore[misc]

        # Cheap fast-path on Content-Length.
        headers = _headers_dict(scope.get("headers") or [])  # type: ignore[arg-type]
        cl = headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > self.limit_bytes:
                    await _send_status(  # type: ignore[arg-type]
                        send,
                        413,
                        b'{"error":"request body too large"}',
                        "application/json",
                    )
                    return None
            except ValueError:
                pass

        # Streamed: wrap receive to count bytes.
        total = 0
        limit = self.limit_bytes

        async def wrapped_receive() -> dict[str, object]:
            nonlocal total
            event = await receive()  # type: ignore[misc]
            if event.get("type") == "http.request":
                body = event.get("body") or b""
                total += len(body)
                if total > limit:
                    raise _BodyTooLargeError()
            return event

        try:
            return await self.app(scope, wrapped_receive, send)  # type: ignore[misc]
        except _BodyTooLargeError:
            await _send_status(  # type: ignore[arg-type]
                send,
                413,
                b'{"error":"request body too large"}',
                "application/json",
            )
            return None


class _BodyTooLargeError(Exception):
    pass


def _headers_dict(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers:
        try:
            out[k.decode("latin-1").lower()] = v.decode("latin-1")
        except Exception:
            continue
    return out


async def _send_status(send: Any, status: int, body: bytes, content_type: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", content_type.encode("latin-1"))],
        }
    )
    await send({"type": "http.response.body", "body": body})
