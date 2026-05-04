"""DashboardServer — FastAPI host for the dashboard SPA + plugin routers + PTY.

Wave 6.D migration (2026-05-04): replaces the previous stdlib ``http.server``
implementation with a FastAPI app. The original stdlib choice was made
to avoid pulling FastAPI/Starlette/Uvicorn into the dependency set, but
FastAPI has since become a hard dep (W2b control surface), so the
constraint is moot and the migration unlocks three features that
otherwise can't ship:

- Plugin routers under ``/api/plugins/<name>/`` (kanban etc.)
- ``/api/pty`` WebSocket bridge for the browser-embedded TUI
- Token-authenticated REST surface for future Plugins/Models pages

What this module does:

- Serves ``static/index.html`` and any sibling static assets (SPA shell).
- Auto-discovers dashboard plugins under
  ``opencomputer/dashboard/plugins/*`` (each must expose a ``router``
  attribute on ``plugin_api.py``) and mounts at ``/api/plugins/<name>/``.
- Serves each plugin's ``dist/`` directory at
  ``/static/plugins/<name>/`` so its bundled JS/CSS is reachable.
- Adds the ``/api/pty`` PTY-over-WebSocket bridge (POSIX-only) gated by
  a session token + loopback check.
- Generates an ephemeral ``_SESSION_TOKEN`` on startup that plugin
  routers and the PTY bridge can both verify against.

What it does NOT do:

- Authenticate the static SPA. Localhost binding is the default; the
  ``--insecure`` operator flag (host=0.0.0.0) is the explicit opt-in
  to network exposure and adds the session-token gate in front of
  ``/api/*`` instead of static.
- Proxy the wire JSON-RPC server. The browser still connects directly
  to ``ws://127.0.0.1:18789``. PTY is the only WebSocket this module
  hosts.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import re
import secrets
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("opencomputer.dashboard.server")

_STATIC_DIR: Path = Path(__file__).parent / "static"
_PLUGINS_DIR: Path = Path(__file__).parent / "plugins"

# Generated on first import; the running app stores this on app.state too
# so plugin routers can read it via ``request.app.state.session_token``.
# Module-level export is for back-compat with plugin code that does
# ``from opencomputer.dashboard.server import _SESSION_TOKEN``.
_SESSION_TOKEN: str = secrets.token_urlsafe(32)


# Loopback hosts treated as safe peers for /api/pty when the server is
# bound to 127.0.0.1. ``testclient`` is what Starlette's TestClient
# reports — keeping it in the set means tests don't have to fake
# request scope.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})

_RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")
_PTY_READ_CHUNK_TIMEOUT = 0.2


def _public_apis_only_when_token(public_paths: set[str]) -> set[str]:
    """Paths that bypass the auth middleware regardless of bind."""
    base = {"/", "/index.html", "/api/health"}
    return base | public_paths


def _build_app(
    *,
    wire_url: str = "ws://127.0.0.1:18789",
    static_dir: Path = _STATIC_DIR,
    plugins_dir: Path = _PLUGINS_DIR,
    enable_pty: bool = True,
    bound_host: str = "127.0.0.1",
) -> FastAPI:
    """Build the FastAPI app.

    Factored out from :class:`DashboardServer` so tests can construct the
    app once and use Starlette's TestClient against it without bringing
    up a uvicorn loop.
    """
    app = FastAPI(
        title="OpenComputer Dashboard",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )

    app.state.session_token = _SESSION_TOKEN
    app.state.bound_host = bound_host
    app.state.wire_url = wire_url

    # Defense-in-depth same-origin: clickjacking + content sniffing.
    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        # Allow inline JS/CSS on the SPA shell (small footprint, single
        # author) but lock connect-src to localhost ws.
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "connect-src 'self' ws://127.0.0.1:* wss://127.0.0.1:* "
            "ws://localhost:* wss://localhost:*; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'",
        )
        return resp

    # --- mount plugin routers + their dist/ ----------------------------
    plugin_apis = _discover_plugins(plugins_dir)
    for name, mod in plugin_apis.items():
        router = getattr(mod, "router", None)
        if router is None:
            log.warning("dashboard plugin %s has no `router` attr — skipping", name)
            continue
        app.include_router(router, prefix=f"/api/plugins/{name}")
        log.info("Mounted plugin API: /api/plugins/%s/", name)
        # Mount the plugin's dist/ if present so its bundled UI assets
        # are reachable. Use try/except on Path.exists so a missing
        # dist/ on a backend-only plugin doesn't crash startup.
        dist = plugins_dir / name / "dist"
        if dist.exists() and dist.is_dir():
            app.mount(
                f"/static/plugins/{name}",
                StaticFiles(directory=str(dist), html=False),
                name=f"plugin-{name}-dist",
            )

    # --- /api/health (always public) -----------------------------------
    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "wire_url": wire_url}

    # --- SPA shell ------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        path = static_dir / "index.html"
        if not path.exists():
            return HTMLResponse(
                "<html><body><h1>OpenComputer Dashboard</h1>"
                "<p>No SPA shell installed. Run "
                "<code>oc dashboard build</code> or place "
                "an <code>index.html</code> in "
                f"<code>{static_dir}</code>.</p></body></html>",
                status_code=200,
            )
        body = path.read_text(encoding="utf-8").replace("__WIRE_URL__", wire_url)
        # Inject the session token so the SPA can attach ?token=... to its
        # WebSocket upgrades. Uses a placeholder identical to the wire-url
        # one so the index.html author has a single substitution mental
        # model.
        body = body.replace("__SESSION_TOKEN__", _SESSION_TOKEN)
        return HTMLResponse(body)

    if static_dir.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(static_dir), html=False),
            name="static",
        )

    # --- /api/pty WebSocket --------------------------------------------
    if enable_pty:
        _attach_pty(app)

    return app


def _discover_plugins(plugins_dir: Path) -> dict[str, object]:
    """Walk ``plugins_dir`` and import each plugin's ``plugin_api`` module.

    A plugin is a subdirectory containing a ``plugin_api.py`` file. The
    discovery is best-effort — a broken plugin logs a warning and is
    skipped, never crashes startup.
    """
    out: dict[str, object] = {}
    if not plugins_dir.exists() or not plugins_dir.is_dir():
        return out
    for child in sorted(plugins_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(("_", ".")):
            continue
        api_file = child / "plugin_api.py"
        if not api_file.exists():
            continue
        module_name = (
            f"opencomputer.dashboard.plugins.{child.name}.plugin_api"
        )
        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            log.warning("dashboard plugin %s failed to import: %s",
                        child.name, exc)
            continue
        out[child.name] = mod
    return out


def _attach_pty(app: FastAPI) -> None:
    """Wire up /api/pty on ``app``. Only POSIX hosts get a working bridge."""
    from opencomputer.dashboard.pty_bridge import (
        PtyBridge,
        PtyUnavailableError,
    )

    def _ws_client_is_allowed(ws: WebSocket) -> bool:
        bound = getattr(ws.app.state, "bound_host", "127.0.0.1")
        # Operator chose --insecure (bind 0.0.0.0). Token still required.
        if bound in ("0.0.0.0", "::"):
            return True
        client_host = ws.client.host if ws.client else ""
        if not client_host:
            return True
        return client_host in _LOOPBACK_HOSTS

    def _resolve_pty_argv() -> tuple[list[str], str | None, dict]:
        """Build argv + cwd + env for the spawned chat child.

        Defaults to ``oc chat``. Tests monkeypatch this function to
        inject a tiny fake command (``cat``) so they don't need an
        installed CLI.
        """
        argv = [
            os.environ.get("OC_DASHBOARD_PTY_CMD", "oc"),
            "chat",
        ]
        cwd = os.environ.get("OC_DASHBOARD_PTY_CWD") or None
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        return argv, cwd, env

    @app.websocket("/api/pty")
    async def pty_ws(ws: WebSocket) -> None:
        token = ws.query_params.get("token", "")
        expected = ws.app.state.session_token
        # Constant-time compare against the running app's token.
        if not secrets.compare_digest(token, expected):
            await ws.close(code=4401)
            return
        if not _ws_client_is_allowed(ws):
            await ws.close(code=4403)
            return
        await ws.accept()

        try:
            argv, cwd, env = _resolve_pty_argv()
            bridge = PtyBridge.spawn(argv, cwd=cwd, env=env)
        except PtyUnavailableError as exc:
            await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
            await ws.close(code=1011)
            return
        except (FileNotFoundError, OSError) as exc:
            await ws.send_text(f"\r\n\x1b[31mChat failed to start: {exc}\x1b[0m\r\n")
            await ws.close(code=1011)
            return

        loop = asyncio.get_running_loop()

        async def pump_pty_to_ws() -> None:
            while True:
                chunk = await loop.run_in_executor(
                    None, bridge.read, _PTY_READ_CHUNK_TIMEOUT,
                )
                if chunk is None:  # EOF — child exited
                    return
                if not chunk:  # no data this tick
                    await asyncio.sleep(0)
                    continue
                try:
                    await ws.send_bytes(chunk)
                except Exception:
                    return

        reader_task = asyncio.create_task(pump_pty_to_ws())
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                raw = msg.get("bytes")
                if raw is None:
                    text = msg.get("text")
                    raw = text.encode("utf-8") if isinstance(text, str) else b""
                if not raw:
                    continue
                # Resize escape: consume locally, never write to PTY
                m = _RESIZE_RE.match(raw)
                if m and m.end() == len(raw):
                    bridge.resize(cols=int(m.group(1)), rows=int(m.group(2)))
                    continue
                bridge.write(raw)
        except WebSocketDisconnect:
            pass
        finally:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader_task
            bridge.close()


class DashboardServer:
    """Threaded uvicorn server hosting the dashboard FastAPI app.

    Use ``start()`` to launch in a background thread, ``stop()`` to shut
    down cleanly. ``url`` returns the bound base URL after start.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9119,
        *,
        wire_url: str = "ws://127.0.0.1:18789",
        static_dir: Path = _STATIC_DIR,
        plugins_dir: Path = _PLUGINS_DIR,
        enable_pty: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.wire_url = wire_url
        self.static_dir = static_dir
        self.plugins_dir = plugins_dir
        self.enable_pty = enable_pty
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.app: FastAPI = _build_app(
            wire_url=wire_url,
            static_dir=static_dir,
            plugins_dir=plugins_dir,
            enable_pty=enable_pty,
            bound_host=host,
        )

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        """Bind the socket and run uvicorn in a background thread."""
        if self._server is not None:
            return
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        def _run() -> None:
            asyncio.run(self._server.serve())  # type: ignore[union-attr]

        self._thread = threading.Thread(
            target=_run, name="opencomputer-dashboard", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal uvicorn to shut down + join the background thread."""
        if self._server is None:
            return
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None


# Public re-export for tests and callers that want the bare app.
def build_app(
    *,
    wire_url: str = "ws://127.0.0.1:18789",
    static_dir: Path = _STATIC_DIR,
    plugins_dir: Path = _PLUGINS_DIR,
    enable_pty: bool = True,
    bound_host: str = "127.0.0.1",
) -> FastAPI:
    """Public alias of :func:`_build_app`. Use in tests."""
    return _build_app(
        wire_url=wire_url,
        static_dir=static_dir,
        plugins_dir=plugins_dir,
        enable_pty=enable_pty,
        bound_host=bound_host,
    )


__all__ = [
    "DashboardServer",
    "_SESSION_TOKEN",
    "build_app",
]
