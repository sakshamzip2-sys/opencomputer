"""DashboardServer — stdlib http.server hosting the dashboard SPA.

Phase 8.A of catch-up plan. We deliberately use the stdlib so this
phase doesn't add FastAPI/Starlette/Uvicorn to the dependency set —
the page is small, traffic is single-user localhost, and the wire
server (already shipped) handles the real-time JSON-RPC over WebSocket.

What this module does:

- Serve ``static/index.html`` and any sibling static assets.
- 404 anything outside ``static/``.
- Inject a small ``<script>`` snippet at page-load time exposing the
  wire-server URL (configurable via constructor arg) so the SPA
  doesn't have to hardcode it.

What it does NOT do:

- Proxy WebSocket traffic. The browser connects directly to
  ``ws://127.0.0.1:18789`` — the existing wire server. Two ports,
  one for static (this), one for ws (wire). Simpler than reverse-
  proxying inside Python.
- Authenticate. Localhost-only is the default. Non-localhost binding
  requires the ``dashboard.bind_external`` consent capability — caller
  enforces this before constructing the server.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

_STATIC_DIR: Path = Path(__file__).parent / "static"


def make_handler(
    static_dir: Path = _STATIC_DIR,
    *,
    wire_url: str = "ws://127.0.0.1:18789",
) -> type[http.server.BaseHTTPRequestHandler]:
    """Return an HTTP handler class bound to ``static_dir`` + ``wire_url``.

    Returned by a factory so we can capture the configuration in a
    closure without subclassing tricks at the call site.
    """
    class Handler(http.server.BaseHTTPRequestHandler):
        # Serve everything from static_dir, hand-coded routes:
        #   GET /          → index.html with wire_url injected
        #   GET /static/*  → file under static_dir (basic mime types)
        #   anything else  → 404

        # Suppress the verbose default logging — caller redirects
        # to its own logger.
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/" or path == "/index.html":
                return self._serve_index()
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            self._send_404()

        def _serve_index(self) -> None:
            index = static_dir / "index.html"
            if not index.exists():
                self._send_404()
                return
            content = index.read_text()
            # Inject wire_url as a JS global so the SPA picks it up
            # without hardcoding. Done with simple textual replace —
            # template engines are overkill for one variable.
            content = content.replace(
                "__WIRE_URL__", wire_url,
            )
            body = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_csp_headers()
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, rel: str) -> None:
            # Reject path-traversal
            target = (static_dir / rel).resolve()
            if not str(target).startswith(str(static_dir.resolve())):
                self._send_404()
                return
            if not target.exists() or not target.is_file():
                self._send_404()
                return
            data = target.read_bytes()
            mime = _mime_for(rel)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self._send_csp_headers()
            self.end_headers()
            self.wfile.write(data)

        def _send_csp_headers(self) -> None:
            # Defense-in-depth even on localhost: prevent the SPA from
            # being framed elsewhere (clickjacking) and lock CSP to
            # localhost ws + same-origin assets.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            csp = (
                "default-src 'self'; "
                "connect-src 'self' ws://127.0.0.1:* wss://127.0.0.1:*; "
                "style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'"
            )
            self.send_header("Content-Security-Policy", csp)

        def _send_404(self) -> None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")

    return Handler


def _mime_for(name: str) -> str:
    """Tiny mime-type table for the few file types the SPA uses."""
    if name.endswith(".css"):
        return "text/css; charset=utf-8"
    if name.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if name.endswith(".html"):
        return "text/html; charset=utf-8"
    if name.endswith(".svg"):
        return "image/svg+xml"
    if name.endswith(".png"):
        return "image/png"
    return "application/octet-stream"


class DashboardServer:
    """Threaded HTTP server hosting the dashboard SPA.

    Use ``start()`` to launch in a background thread, ``stop()`` to
    shut down cleanly. ``url`` returns the bound base URL after start.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9119,
        *,
        wire_url: str = "ws://127.0.0.1:18789",
        static_dir: Path = _STATIC_DIR,
    ) -> None:
        self.host = host
        self.port = port
        self.wire_url = wire_url
        self.static_dir = static_dir
        self._httpd: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        """Bind the socket and run in a background thread."""
        if self._httpd is not None:
            return
        handler_cls = make_handler(self.static_dir, wire_url=self.wire_url)
        # Allow port reuse so quick restarts don't trip TIME_WAIT.
        socketserver.TCPServer.allow_reuse_address = True
        self._httpd = socketserver.TCPServer((self.host, self.port), handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="opencomputer-dashboard",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Shut down the server thread cleanly."""
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._httpd = None
        self._thread = None
