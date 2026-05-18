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
#
# OC_DASHBOARD_TOKEN env override (tryopencomputer.com platform integration,
# Phase 1a — 2026-05-18):
#   When set, the value is used verbatim and is NOT regenerated on subsequent
#   process restarts. This is the **only** safe shape for production VM
#   deployments where the platform records the token in its DB and routes
#   requests using it — a fresh random token per process restart would
#   silently invalidate the platform's view. Falls back to a random token
#   for local / standalone use.
#
#   See: OpenComputer/docs/SECURITY-INVARIANTS.md invariant #4 and
#        OpenComputer/docs/plans/tryopencomputer-platform-build-2026-05-18.md
#        Phase 1a.
_SESSION_TOKEN: str = (
    os.environ.get("OC_DASHBOARD_TOKEN")
    or secrets.token_urlsafe(32)
)


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

    # --- v1 domain-split routers ---------------------------------------
    # 17 routers under /api/v1/* — see opencomputer/dashboard/routes/.
    # Stub modules are registered alongside the populated ones so the
    # SPA's first-load `/api/v1/status` works before later PRs land.
    from opencomputer.dashboard.routes import ALL_ROUTERS

    for v1_router in ALL_ROUTERS:
        app.include_router(v1_router)

    # --- /api/health (always public) -----------------------------------
    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "wire_url": wire_url}

    # --- /health (gateway-shape alias, 2026-05-12) ---------------------
    # Hermes-workspace's gateway-capabilities probe (`oc workspace`'s
    # Node-side capability discovery) hits the bare ``/health`` path, not
    # ``/api/health`` or ``/v1/health``. Without this alias the workspace
    # would render the dashboard as "disconnected" even though OC is
    # serving every other endpoint. Same payload as ``/api/health`` so
    # any tooling already keyed on its shape keeps working.
    @app.get("/health")
    async def health_alias() -> dict:
        return {"ok": True, "wire_url": wire_url, "status": "ok"}

    # --- SPA shell + Wave 6.D-α static pages ---------------------------
    def _render_html(path: Path) -> Response:
        if not path.exists():
            return HTMLResponse(
                f"<html><body><h1>OpenComputer Dashboard</h1>"
                f"<p>{path.name} not found in {path.parent}.</p></body></html>",
                status_code=404,
            )
        body = path.read_text(encoding="utf-8").replace("__WIRE_URL__", wire_url)
        # Inject the session token so the SPA can attach Bearer/?token to
        # API calls and WebSocket upgrades.
        body = body.replace("__SESSION_TOKEN__", _SESSION_TOKEN)
        return HTMLResponse(body)

    # --- SPA at / (post-2026-05-07 — Vite-built dashboard) -------------
    # Built by `cd OpenComputer/ui-web && npm run build`; outputs to
    # `static/spa/`. When present, `/` serves the SPA's index.html and
    # any non-/api, non-/static, non-/assets path falls through to it
    # so React Router handles the route on hard refresh.
    _SPA_DIR = static_dir / "spa"

    if _SPA_DIR.exists() and (_SPA_DIR / "index.html").exists():
        # Hashed assets — Vite emits content-hashed filenames so cache
        # control is safe. _render_html only handles the index shell.
        _ASSETS_DIR = _SPA_DIR / "assets"
        if _ASSETS_DIR.exists():
            app.mount(
                "/assets",
                StaticFiles(directory=str(_ASSETS_DIR), html=False),
                name="spa-assets",
            )
        # Synced @nous-research/ui fonts + ds-assets (sync-assets script).
        for sub in ("fonts", "ds-assets"):
            d = _SPA_DIR / sub
            if d.exists():
                app.mount(
                    f"/{sub}",
                    StaticFiles(directory=str(d), html=False),
                    name=f"spa-{sub}",
                )

        @app.get("/", response_class=HTMLResponse)
        async def index() -> Response:
            return _render_html(_SPA_DIR / "index.html")
    else:
        # No SPA build artifact — fall back to the legacy `static/index.html`.
        @app.get("/", response_class=HTMLResponse)
        async def index() -> Response:  # type: ignore[no-redef]
            return _render_html(static_dir / "index.html")

    @app.get("/static/plugins.html", response_class=HTMLResponse)
    async def plugins_page() -> Response:
        return _render_html(static_dir / "plugins.html")

    @app.get("/static/models.html", response_class=HTMLResponse)
    async def models_page() -> Response:
        return _render_html(static_dir / "models.html")

    # Hermes-followup A1 (2026-05-07) — LLM calls real-time tracker.
    @app.get("/static/llm-calls.html", response_class=HTMLResponse)
    async def llm_calls_page() -> Response:
        return _render_html(static_dir / "llm-calls.html")

    @app.get("/api/llm-calls/recent")
    async def llm_calls_recent(limit: int = 50) -> dict:
        """Return the most recent ``llm_calls`` rows for the live tracker.

        Reads the active profile's ``sessions.db``. Hardens against:
        - missing DB / pre-v13 schema (returns empty list).
        - bad ``limit`` (clamped to 1..500).
        """
        from opencomputer.agent.config import default_config
        from opencomputer.agent.state import SessionDB

        clamped = max(1, min(int(limit or 50), 500))
        db_path = default_config().home / "sessions.db"
        if not db_path.exists():
            return {"rows": [], "limit": clamped}
        db = SessionDB(db_path)
        try:
            with db._connect() as conn:  # noqa: SLF001 — internal helper, dashboard read-only
                rows = conn.execute(
                    "SELECT id, session_id, ts, provider, model, "
                    "input_tokens, output_tokens, cost_usd, batch "
                    "FROM llm_calls ORDER BY ts DESC LIMIT ?",
                    (clamped,),
                ).fetchall()
        except Exception:
            return {"rows": [], "limit": clamped}
        return {
            "rows": [dict(r) for r in rows],
            "limit": clamped,
        }

    # Hermes-followup A1 — gateway-restart endpoint. Token-gated by
    # the existing /api/* auth guard. Reads the gateway daemon's PID
    # from a pidfile and signals SIGUSR1; the daemon's signal handler
    # is responsible for re-exec. Refuses to signal if no pidfile is
    # found — we MUST NOT kill the dashboard process itself, which
    # may be the same Python process when ``oc gateway`` and the
    # dashboard are co-hosted.
    @app.post("/api/gateway/restart")
    async def gateway_restart() -> dict:
        import os
        import signal
        from pathlib import Path as _Path

        pidfile_candidates = [
            _Path.home() / ".opencomputer" / "default" / "gateway.pid",
            _Path.home() / ".opencomputer" / "gateway.pid",
        ]
        target_pid: int | None = None
        for cand in pidfile_candidates:
            try:
                if cand.exists():
                    target_pid = int(cand.read_text().strip())
                    break
            except Exception:
                continue

        if target_pid is None:
            return {
                "ok": False,
                "error": (
                    "no gateway pidfile found at "
                    "~/.opencomputer/[<profile>/]gateway.pid"
                ),
            }
        # Belt-and-braces: never signal our own process — even if a stale
        # pidfile somehow contains our PID, we'd kill the dashboard.
        if target_pid == os.getpid():
            return {
                "ok": False,
                "pid": target_pid,
                "error": (
                    "pidfile points at the dashboard process itself; "
                    "refusing to signal (would kill the UI)"
                ),
            }
        try:
            os.kill(target_pid, signal.SIGUSR1)
            return {"ok": True, "pid": target_pid, "signal": "SIGUSR1"}
        except (ProcessLookupError, PermissionError) as exc:
            return {"ok": False, "pid": target_pid, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    if static_dir.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(static_dir), html=False),
            name="static",
        )

    # --- /api/pty WebSocket --------------------------------------------
    if enable_pty:
        _attach_pty(app)

    # --- SPA route fallback (must come AFTER /api/* + /static/* + /assets/*) ---
    # React Router renders client-side routes like /sessions, /logs.
    # Hard-refreshing one of those would 404 without a fallback that
    # serves index.html and lets the client-side router resolve.
    # Unknown /api/* + /static/* paths are explicitly 404'd so the SPA
    # only catches genuine SPA navigations.
    if _SPA_DIR.exists() and (_SPA_DIR / "index.html").exists():

        @app.get("/{spa_path:path}", response_class=HTMLResponse)
        async def spa_fallback(spa_path: str) -> Response:
            if spa_path.startswith(("api/", "static/", "assets/", "fonts/", "ds-assets/")):
                return Response(status_code=404)
            return _render_html(_SPA_DIR / "index.html")

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
