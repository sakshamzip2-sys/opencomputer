"""Browser-redirect OAuth helper.

Generic PKCE + local-callback-server primitives for OAuth providers that use
the *authorization code grant with browser redirect* (e.g. Google Gemini,
Spotify, GitHub). Complements ``device_code.py`` (RFC 8628 device-code flow)
which has no browser involvement.

Workflow:

    1. ``pair = generate_pkce_pair()``  — random verifier + S256 challenge.
    2. Build the auth URL with ``code_challenge=pair.challenge`` and
       ``redirect_uri=http://localhost:<port>/callback``.
    3. ``open_url(auth_url)`` — open the user's default browser.
    4. ``result = wait_for_redirect_callback(redirect_uri, timeout_seconds=...)``
       — blocks until the browser redirects back; returns
       ``{"code": ..., "state": ..., "error": ..., "error_description": ...}``.
    5. POST the ``code + verifier`` to the provider's token endpoint.

The local server only binds to loopback (127.0.0.1 / localhost). HTTPS is
intentionally NOT supported — modern OAuth specs allow plain HTTP for the
loopback IP since the traffic never leaves the host. PKCE compensates for
the lack of a client secret.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class PKCEPair:
    verifier: str  # 43-128 chars, URL-safe
    challenge: str  # base64url(sha256(verifier))
    method: str = "S256"


def generate_pkce_pair() -> PKCEPair:
    """Generate an RFC 7636 PKCE verifier + S256 challenge.

    The verifier is 43 chars of URL-safe base64 (32 bytes of entropy → 43
    chars after stripping padding). The challenge is base64url(sha256(verifier)).
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return PKCEPair(verifier=verifier, challenge=challenge)


def validate_redirect_uri(redirect_uri: str) -> tuple[str, int, str]:
    """Validate a loopback redirect URI; return (host, port, path).

    Raises ``ValueError`` for non-http schemes, non-loopback hosts, or missing
    explicit port. Path defaults to ``/`` when the URI has no path component.
    """
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http":
        raise ValueError(
            "redirect_uri must use http:// (loopback PKCE) — got "
            f"{parsed.scheme!r}"
        )
    host = parsed.hostname or ""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError(
            f"redirect_uri host must be loopback (127.0.0.1 or localhost), got {host!r}"
        )
    if not parsed.port:
        raise ValueError(
            "redirect_uri must include an explicit port (e.g. http://localhost:8765/callback)"
        )
    return host, parsed.port, parsed.path or "/"


def _make_callback_handler(
    expected_path: str,
) -> tuple[type[BaseHTTPRequestHandler], dict[str, Any]]:
    """Build a one-shot callback handler. Returns (handler_class, result_dict).

    The result dict is shared with the handler — the calling thread reads
    from it once the handler has populated ``code`` or ``error``.
    """
    result: dict[str, Any] = {
        "code": None,
        "state": None,
        "error": None,
        "error_description": None,
    }

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server convention
            parsed = urlparse(self.path)
            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not the OAuth callback path.")
                return

            params = parse_qs(parsed.query)
            result["code"] = params.get("code", [None])[0]
            result["state"] = params.get("state", [None])[0]
            result["error"] = params.get("error", [None])[0]
            result["error_description"] = params.get("error_description", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if result["error"]:
                body = (
                    "<html><body style='font-family:system-ui;padding:2em'>"
                    "<h1>Authorization failed.</h1>"
                    "<p>You can close this tab and return to the terminal.</p>"
                    "</body></html>"
                )
            else:
                body = (
                    "<html><body style='font-family:system-ui;padding:2em'>"
                    "<h1>Authorization received.</h1>"
                    "<p>You can close this tab and return to the terminal.</p>"
                    "</body></html>"
                )
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return  # silence default stderr noise

    return _CallbackHandler, result


def wait_for_redirect_callback(
    redirect_uri: str,
    *,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Bind a one-shot HTTP server on the loopback redirect, await the callback.

    Blocks the calling thread up to ``timeout_seconds`` waiting for the OAuth
    provider to redirect the user's browser back to ``redirect_uri`` with
    either ``?code=...`` or ``?error=...``. Returns the parsed dict
    (keys: code / state / error / error_description). Raises ``TimeoutError``
    if the deadline elapses without a hit.

    Only the configured ``redirect_uri.path`` is honored — other paths return
    HTTP 404 so a stray browser request doesn't satisfy the wait.
    """
    host, port, path = validate_redirect_uri(redirect_uri)
    handler_cls, result = _make_callback_handler(path)

    class _ReuseHTTPServer(HTTPServer):
        allow_reuse_address = True

    try:
        server = _ReuseHTTPServer((host, port), handler_cls)
    except OSError as exc:
        raise RuntimeError(
            f"Could not bind OAuth callback server on {host}:{port}: {exc}"
        ) from exc

    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    deadline = time.time() + max(0.5, timeout_seconds)
    try:
        while time.time() < deadline:
            if result["code"] or result["error"]:
                return result
            time.sleep(0.05)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)

    raise TimeoutError(
        f"OAuth authorization timed out after {timeout_seconds}s — "
        f"no callback received at {redirect_uri}."
    )


def open_url(url: str) -> bool:
    """Open ``url`` in the user's default browser. Returns success."""
    try:
        return webbrowser.open(url, new=2)
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "PKCEPair",
    "generate_pkce_pair",
    "open_url",
    "validate_redirect_uri",
    "wait_for_redirect_callback",
]
