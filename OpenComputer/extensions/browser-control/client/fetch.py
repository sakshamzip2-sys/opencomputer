"""Dual-transport request helper for the browser control surface.

Two transports:

  - **HTTP** — when ``path_or_url`` is an absolute ``http(s)://`` URL,
    the call goes out through ``httpx.AsyncClient.request`` so the
    sandbox / remote-gateway use case works.
  - **In-process dispatcher** — when ``path_or_url`` is a path (e.g.
    ``/snapshot``, ``/tabs``), the call routes through the
    in-process FastAPI dispatcher (server.dispatcher) so the local
    AgentLoop can talk to the same control surface without paying
    socket overhead.

Behavior choices that mirror OpenClaw:

  - **No retries.** 4xx/5xx surface immediately so the agent sees real
    errors. The model-hint string appended to wrapped errors warns the
    LLM not to loop.
  - **No typed error hierarchy on the client.** Status mapping happens
    at the server boundary; the client surfaces ``BrowserServiceError``
    with the server-supplied message verbatim (except 429, which gets a
    static rate-limit hint — never reflect upstream text on rate limit).
  - **Per-call timeouts.** Caller passes ``timeout`` (seconds, float).
    Default 5s if not provided.
  - **Auth is loopback-only.** ``client.auth.inject_auth_headers`` is the
    single funnel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from .._utils.errors import BrowserServiceError
from ..server.auth import BrowserAuth
from ..server.dispatcher import dispatch_browser_control_request
from .auth import inject_auth_headers, is_loopback_host

_log = logging.getLogger("opencomputer.browser_control.client.fetch")

#: Carried verbatim into wrapped errors so the LLM is told not to retry
#: blindly when the control service is unreachable. Static prompt text;
#: load-bearing per the OpenClaw deep dive.
_BROWSER_TOOL_MODEL_HINT = (
    " Do NOT retry this call in a loop; surface the failure and either "
    "wait for the operator or escalate to the user."
)

_RATE_LIMIT_HINT = (
    "Browser service rate limit reached. "
    "Wait for the current session to complete, or retry later."
)

Transport = Literal["http", "dispatcher"]

#: Module-level default FastAPI app used by the dispatcher transport.
#: ``plugin.py`` calls :func:`set_default_dispatcher_app` once the server
#: is built so callers that pass path-only URLs don't need to thread the
#: app through every call. ``None`` means dispatcher transport is
#: unavailable (callers must use absolute URLs).
_default_dispatcher_app: Any = None


def set_default_dispatcher_app(app: Any) -> None:
    """Register the FastAPI app the dispatcher transport routes through."""
    global _default_dispatcher_app
    _default_dispatcher_app = app


def get_default_dispatcher_app() -> Any:
    return _default_dispatcher_app


def _is_absolute_http(s: str) -> bool:
    if not s:
        return False
    lower = s.strip().lower()
    return lower.startswith("http://") or lower.startswith("https://")


def _split_path_and_query(path: str) -> tuple[str, dict[str, list[str]]]:
    """Split ``/foo?bar=1&bar=2`` into (``"/foo"``, ``{"bar": ["1", "2"]}``)."""
    if "?" not in path:
        return path, {}
    base, _, raw_qs = path.partition("?")
    out: dict[str, list[str]] = {}
    for chunk in raw_qs.split("&"):
        if not chunk:
            continue
        k, _eq, v = chunk.partition("=")
        out.setdefault(k, []).append(v)
    return base, out


async def fetch_browser_json(
    method: str,
    path_or_url: str,
    *,
    body: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 5.0,
    auth: BrowserAuth | None = None,
    dispatcher_app: Any = None,
    transport: Transport | None = None,
) -> Any:
    """Run a single browser-control request, returning the JSON body.

    Path or absolute-URL forks the transport.

    Raises:
        BrowserServiceError: on any non-2xx, including the special 429
            rate-limit static-hint case. On unreachable / connection
            failure (HTTP path only), the message is wrapped with
            ``"Can't reach the browser control service: ..."``.
    """
    if transport == "http" or (transport is None and _is_absolute_http(path_or_url)):
        return await _fetch_http(
            method,
            path_or_url,
            body=body,
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    return await _fetch_dispatcher(
        method,
        path_or_url,
        body=body,
        headers=headers,
        timeout=timeout,
        auth=auth,
        dispatcher_app=dispatcher_app or _default_dispatcher_app,
    )


async def _fetch_http(
    method: str,
    url: str,
    *,
    body: Any,
    headers: Mapping[str, str] | None,
    timeout: float,
    auth: BrowserAuth | None,
) -> Any:
    try:
        import httpx  # local import — keeps the dispatcher path import-free
    except ImportError as exc:  # pragma: no cover
        raise BrowserServiceError(
            "httpx is not installed. The HTTP transport for the browser "
            "control client requires httpx (pip install opencomputer[browser])."
            + _BROWSER_TOOL_MODEL_HINT
        ) from exc

    parts = urlsplit(url)
    if not is_loopback_host(parts.hostname or ""):
        # Defense in depth: refuse non-loopback targets. The control
        # service is loopback-only; this guards against accidental SSRF.
        raise BrowserServiceError(
            f"Refusing to call non-loopback browser control URL: {url!r}"
            + _BROWSER_TOOL_MODEL_HINT
        )

    final_headers = inject_auth_headers(headers, auth=auth, url=url)
    json_body: Any | None = body if body is not None else None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method.upper(),
                url,
                json=json_body if not isinstance(body, bytes) else None,
                content=body if isinstance(body, bytes) else None,
                headers=final_headers,
            )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise BrowserServiceError(
            f"Can't reach the browser control service at {url}: {exc}"
            + _BROWSER_TOOL_MODEL_HINT
        ) from exc
    except httpx.ReadTimeout as exc:
        raise BrowserServiceError(
            f"Browser control service timed out after {timeout:g}s: {url}"
            + _BROWSER_TOOL_MODEL_HINT
        ) from exc
    except httpx.HTTPError as exc:
        raise BrowserServiceError(
            f"Browser control transport error: {exc}" + _BROWSER_TOOL_MODEL_HINT
        ) from exc

    return _interpret_response(resp.status_code, resp.content, resp.headers)


async def _fetch_dispatcher(
    method: str,
    path: str,
    *,
    body: Any,
    headers: Mapping[str, str] | None,
    timeout: float,
    auth: BrowserAuth | None,
    dispatcher_app: Any,
) -> Any:
    if dispatcher_app is None:
        raise BrowserServiceError(
            "In-process dispatcher is not registered; call "
            "set_default_dispatcher_app(app) or pass dispatcher_app= "
            "or use an absolute http(s) URL."
            + _BROWSER_TOOL_MODEL_HINT
        )

    bare_path, query = _split_path_and_query(path)

    body_for_dispatch: Any = body
    if isinstance(body, str):
        try:
            body_for_dispatch = json.loads(body)
        except (ValueError, TypeError):
            pass  # leave as raw string

    coro = dispatch_browser_control_request(
        dispatcher_app,
        method=method,
        path=bare_path,
        body=body_for_dispatch,
        query=query or None,
        auth=auth,
        extra_headers=dict(headers or {}),
    )

    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError as exc:
        raise BrowserServiceError(
            f"In-process dispatcher timed out after {timeout:g}s: {path}"
            + _BROWSER_TOOL_MODEL_HINT
        ) from exc

    return _interpret_dispatch_result(result.status, result.body, result.headers)


def _interpret_response(status: int, raw: bytes | str, headers: Mapping[str, str]) -> Any:
    """Translate an HTTP response into JSON or raise BrowserServiceError."""
    content_type = ""
    for k, v in headers.items():
        if k.lower() == "content-type":
            content_type = v.lower()
            break

    if 200 <= status < 300:
        if not raw:
            return {}
        if content_type.startswith("application/json"):
            try:
                return json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise BrowserServiceError(
                    f"Browser control returned malformed JSON: {exc}"
                ) from exc
        # Non-JSON success — return bytes verbatim
        return raw

    # Error status: 429 special-cased, every other status surfaces the
    # body verbatim (no template message).
    if status == 429:
        raise BrowserServiceError(_RATE_LIMIT_HINT, status=status)

    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    body_obj: Any = None
    if content_type.startswith("application/json"):
        try:
            body_obj = json.loads(text)
        except ValueError:
            body_obj = None

    if isinstance(body_obj, dict):
        raise BrowserServiceError.from_response(status, body_obj)

    raise BrowserServiceError(text or f"HTTP {status}", status=status)


def _interpret_dispatch_result(
    status: int,
    body: Any,
    headers: Mapping[str, str],
) -> Any:
    if 200 <= status < 300:
        return body if body is not None else {}

    if status == 429:
        raise BrowserServiceError(_RATE_LIMIT_HINT, status=status)

    if isinstance(body, dict):
        raise BrowserServiceError.from_response(status, body)
    if isinstance(body, (bytes, bytearray)):
        text = body.decode("utf-8", errors="replace")
        raise BrowserServiceError(text or f"HTTP {status}", status=status)

    raise BrowserServiceError(str(body) or f"HTTP {status}", status=status)


__all__ = [
    "Transport",
    "fetch_browser_json",
    "get_default_dispatcher_app",
    "set_default_dispatcher_app",
]
