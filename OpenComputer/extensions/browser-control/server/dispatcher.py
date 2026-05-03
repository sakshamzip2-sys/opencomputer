"""In-process dispatcher — the dual-transport other half.

The same handler functions are called from both:

  - HTTP routes (FastAPI) — caller is uvicorn/httpx/curl over loopback.
  - In-process direct invocation — caller is the local AgentLoop in
    the same Python process.

The dispatcher exposes ``dispatch_browser_control_request(method, path,
*, body, query, profile, auth, headers)`` that returns a
``DispatchResult`` (status + body dict). Internally it builds a small
ASGI scope and routes through the FastAPI app — that way the HTTP path
and the in-process path go through the **exact same** middleware stack
(CSRF / auth / body-limit) and route handlers.

Production wiring (W3) plugs this in as the local-loopback fast path
while still exposing the HTTP server for sandboxed callers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from .auth import BrowserAuth


@dataclass(slots=True)
class DispatchResult:
    status: int
    body: dict[str, Any] | bytes
    headers: dict[str, str]


async def dispatch_browser_control_request(
    app: Any,
    *,
    method: str,
    path: str,
    body: dict[str, Any] | bytes | None = None,
    query: dict[str, Any] | None = None,
    auth: BrowserAuth | None = None,
    extra_headers: dict[str, str] | None = None,
    request_origin: str = "http://127.0.0.1",
) -> DispatchResult:
    """Run an in-process request through the same FastAPI app the HTTP
    server uses.

    Sets ``Origin`` and ``Sec-Fetch-Site: same-origin`` so the CSRF
    middleware passes. If ``auth`` has a token, ``Authorization: Bearer``
    is set automatically.
    """
    raw_body: bytes
    headers: dict[str, str] = {
        "host": "127.0.0.1",
        "origin": request_origin,
        "sec-fetch-site": "same-origin",
    }

    if isinstance(body, (dict, list)):
        raw_body = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    elif isinstance(body, bytes):
        raw_body = body
    elif body is None:
        raw_body = b""
    else:
        raw_body = str(body).encode("utf-8")

    headers["content-length"] = str(len(raw_body))

    if auth is not None and auth.token:
        headers["authorization"] = f"Bearer {auth.token}"
    elif auth is not None and auth.password:
        headers["x-opencomputer-password"] = auth.password

    if extra_headers:
        for k, v in extra_headers.items():
            headers[k.lower()] = v

    qs = urlencode(query or {}, doseq=True).encode("ascii") if query else b""

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": qs,
        "root_path": "",
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 0),
    }

    sent_body = raw_body
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": sent_body, "more_body": False}

    captured_status: list[int] = []
    captured_headers: list[tuple[bytes, bytes]] = []
    captured_body = bytearray()

    async def send(event: dict[str, Any]) -> None:
        if event.get("type") == "http.response.start":
            captured_status.append(int(event.get("status", 200)))
            captured_headers.extend(event.get("headers") or [])
        elif event.get("type") == "http.response.body":
            chunk = event.get("body") or b""
            captured_body.extend(chunk)

    await app(scope, receive, send)

    status = captured_status[0] if captured_status else 500
    hdrs = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in captured_headers}
    raw_resp = bytes(captured_body)
    if hdrs.get("content-type", "").startswith("application/json"):
        try:
            body_out: Any = json.loads(raw_resp.decode("utf-8") or "null")
        except Exception:
            body_out = raw_resp
    else:
        body_out = raw_resp
    return DispatchResult(status=status, body=body_out, headers=hdrs)
