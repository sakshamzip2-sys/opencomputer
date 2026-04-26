"""OAuth 2.1 PKCE flow for MCP servers (Round 2 P-6).

Implements an interactive Authorization-Code-with-PKCE flow against an
MCP server's OAuth provider. The flow:

1. Generate a high-entropy PKCE ``code_verifier``
   (``secrets.token_urlsafe(64)`` — 256-bit equivalent, well above the
   43-char minimum in RFC 7636 §4.1).
2. Derive the SHA-256 ``code_challenge`` (S256 method).
3. Generate a CSRF ``state`` (``secrets.token_urlsafe(32)``).
4. Bind a one-shot HTTP server to ``127.0.0.1:0`` (kernel-picked
   ephemeral port) — **never** ``0.0.0.0`` and **never** ``localhost``
   (which can resolve to IPv6 on some hosts and break the redirect).
5. Open the user's browser to the authorization URL with
   ``code_challenge``, ``code_challenge_method=S256``, ``state``,
   ``redirect_uri``. If browser launch fails, print the URL so the user
   can paste it manually.
6. Wait up to ``timeout_s`` (default 300s) for the OAuth provider to
   redirect back. Constant-time-compare the returned ``state`` against
   the expected value (CSRF defense — never use ``==``).
7. POST ``code`` + ``code_verifier`` to the token endpoint. Return the
   provider's JSON response (``access_token`` / ``refresh_token`` /
   ``expires_in`` / ``scope`` / etc.).

The ephemeral server is shut down in a ``try/finally`` block so the
listening socket is freed even when the flow raises.

Token persistence is the **caller's** responsibility — the typical
caller is the ``opencomputer mcp oauth-login`` CLI command, which
hands the returned dict to :class:`opencomputer.mcp.oauth.OAuthTokenStore`.

References:
    - RFC 7636 (PKCE):  https://datatracker.ietf.org/doc/html/rfc7636
    - OAuth 2.1 draft:  https://oauth.net/2.1/
    - MCP authorization spec:
      https://modelcontextprotocol.io/specification/draft/basic/authorization
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import logging
import queue
import secrets
import socketserver
import threading
import urllib.parse
import webbrowser
from typing import Any

import httpx

logger = logging.getLogger("opencomputer.mcp.oauth_pkce")


# ─── Errors ───────────────────────────────────────────────────────


class OAuthFlowError(Exception):
    """Base class for PKCE flow errors."""


class OAuthFlowTimeout(OAuthFlowError):  # noqa: N818 — domain term, not "*Error"
    """The OAuth provider did not redirect back within the timeout window."""


class OAuthCallbackError(OAuthFlowError):
    """The callback request was malformed (e.g. missing ``code``)."""


class OAuthStateMismatch(OAuthFlowError):  # noqa: N818 — domain term, not "*Error"
    """The ``state`` returned by the provider did not match the value we sent.

    This is a CSRF defense — when raised, refuse to exchange the code.
    """


# ─── PKCE primitives ──────────────────────────────────────────────


def _make_verifier() -> str:
    """Return a fresh PKCE ``code_verifier``.

    ``secrets.token_urlsafe(64)`` returns ~86 URL-safe base64 chars
    (well over the 43-char minimum in RFC 7636 §4.1, well under the
    128-char maximum). 64 bytes of entropy = 256 bits, comfortably
    safe against brute force.
    """
    return secrets.token_urlsafe(64)


def _make_challenge(verifier: str) -> str:
    """Derive the S256 ``code_challenge`` from a verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _make_state() -> str:
    """Return a fresh CSRF ``state`` value (~43 URL-safe chars)."""
    return secrets.token_urlsafe(32)


# ─── Callback HTTP server ─────────────────────────────────────────


def _build_callback_server(
    result_queue: queue.Queue[tuple[str | None, str | None]],
) -> socketserver.TCPServer:
    """Bind a one-shot HTTP server to ``127.0.0.1:0`` for the OAuth callback.

    Hard requirements:
        - Bind ``127.0.0.1`` ONLY (never ``0.0.0.0``; never ``localhost``).
        - Kernel-picked ephemeral port via ``("127.0.0.1", 0)``.
        - The handler captures ``code`` + ``state`` into ``result_queue``.
        - ``log_message`` is silenced so the noisy default access log does
          not mangle the user's terminal during the browser dance.
    """

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = params.get("code", [None])[0]
            returned_state = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"OAuth complete. You may close this window and return to the terminal."
            )
            try:
                result_queue.put_nowait((code, returned_state))
            except queue.Full:
                # Already received a callback; ignore retries (browser refresh).
                pass

        def log_message(self, *args: object, **kwargs: object) -> None:  # noqa: D401
            """Silence default access log."""

    # ``allow_reuse_address`` lets an immediate re-run rebind even if
    # the kernel hasn't fully released the previous ephemeral port.
    # Subclass instead of mutating the base class — the previous code
    # set ``socketserver.TCPServer.allow_reuse_address = True`` on the
    # CLASS, leaking the change to every other TCPServer instantiation
    # in the process (e.g. the dashboard server). Subclass scopes it
    # to *this* server only.
    class _ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    return _ReusableTCPServer(("127.0.0.1", 0), _Handler)


# ─── Top-level entry point ────────────────────────────────────────


def run_pkce_flow(
    *,
    authorization_url: str,
    token_url: str,
    client_id: str,
    scope: str = "",
    timeout_s: int = 300,
    extra_authorize_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the OAuth 2.1 Authorization-Code + PKCE flow end-to-end.

    Args:
        authorization_url: The provider's ``/authorize`` endpoint.
        token_url: The provider's ``/token`` endpoint.
        client_id: The OAuth client id registered with the provider.
        scope: Space-separated scope string (provider-specific). Optional.
        timeout_s: How long to wait for the browser callback. Default 300s
            (5 minutes). Must be >0; small values are honoured for tests.
        extra_authorize_params: Extra query-string parameters to merge
            into the ``/authorize`` URL (rarely needed; some providers
            require ``audience`` / ``prompt`` / etc.).

    Returns:
        The provider's token response JSON, typically::

            {
                "access_token": "...",
                "refresh_token": "...",      # optional
                "token_type": "Bearer",
                "expires_in": 3600,           # optional
                "scope": "...",               # optional
            }

    Raises:
        OAuthFlowTimeout: No callback within ``timeout_s``.
        OAuthCallbackError: Callback was missing ``code``.
        OAuthStateMismatch: Returned ``state`` did not match (CSRF).
        httpx.HTTPStatusError: Token endpoint returned non-2xx.
    """
    if timeout_s <= 0:
        raise ValueError(f"timeout_s must be > 0 (got {timeout_s})")

    verifier = _make_verifier()
    challenge = _make_challenge(verifier)
    state = _make_state()

    # The result_queue is bounded to one entry — the first callback
    # wins; any subsequent browser refresh is ignored quietly.
    result_queue: queue.Queue[tuple[str | None, str | None]] = queue.Queue(maxsize=1)
    server = _build_callback_server(result_queue)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    auth_params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        auth_params["scope"] = scope
    if extra_authorize_params:
        auth_params.update(extra_authorize_params)

    auth_url = f"{authorization_url}?{urllib.parse.urlencode(auth_params)}"

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        opened = webbrowser.open(auth_url)
        if not opened:
            # Fallback: surface the URL for manual paste rather than
            # crashing. Headless / SSH sessions hit this path.
            print(
                "Could not auto-open a browser. Visit this URL to authorize:\n"
                f"  {auth_url}"
            )
        else:
            logger.info("Opened browser to OAuth authorize URL on port %d", port)

        try:
            code, returned_state = result_queue.get(timeout=timeout_s)
        except queue.Empty as exc:
            raise OAuthFlowTimeout(
                f"no OAuth callback received within {timeout_s}s"
            ) from exc

        if not code:
            raise OAuthCallbackError(
                "OAuth callback was missing 'code' parameter (user may have "
                "denied access or the provider returned an error)"
            )

        # Constant-time compare — equality is a CSRF-defense correctness
        # property and using ``==`` would leak length / prefix bits.
        if not secrets.compare_digest(state, returned_state or ""):
            raise OAuthStateMismatch(
                "CSRF defense: OAuth callback state did not match the value "
                "we sent. Discarding the authorization code."
            )

        # Exchange the authorization code for a token.
        token_payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        resp = httpx.post(token_url, data=token_payload, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    finally:
        # Always shut the callback server down — even if the user closes
        # the browser tab or the provider hangs. The daemon thread will
        # exit once ``serve_forever`` returns.
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        try:
            server.server_close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


__all__ = [
    "OAuthCallbackError",
    "OAuthFlowError",
    "OAuthFlowTimeout",
    "OAuthStateMismatch",
    "run_pkce_flow",
]
