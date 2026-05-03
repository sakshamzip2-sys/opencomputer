"""Server lifecycle — startup + shutdown.

Startup (12 steps, condensed):

  1. Resolve config → ResolvedBrowserConfig.
  2. Bail if not enabled.
  3. ensure_browser_control_auth → BrowserAuth.
  4. Create BrowserServerState (no Chrome yet — lazy on first request).
  5. Build the BrowserRouteContext (state, auth, driver, tab_backend).
  6. create_app(ctx).
  7. Bind a uvicorn Server config; bind to 127.0.0.1 only.
  8. Start uvicorn in the background.
  9. Wait for the server to be reachable (/internal/ping or socket probe).
 10. Stash port (in case caller used port=0).
 11. Return BrowserServerStartResult{state, app, server, auth, port}.

Shutdown (6 steps):

  1. Signal uvicorn to stop.
  2. For each profile in state.profiles → teardown_profile(driver=...).
  3. Wait for uvicorn to finish.
  4. Clear state.profiles.
  5. Close any cached Playwright connection (lazy import).
  6. Drop state references.

The startup function returns a ``BrowserServerHandle`` that exposes
``stop()``; tests use it for tear-down. Production wiring (W3) hooks
this into the OpenComputer plugin lifecycle.

For v0.1 we use uvicorn directly (no separate process); this lets the
in-process dispatcher and HTTP path share the same FastAPI app instance.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ..profiles.config import ResolvedBrowserConfig, SsrfPolicy
from ..server_context import BrowserServerState, ProfileDriver, teardown_profile
from ..server_context.tab_ops import TabOpsBackend
from .app import create_app
from .auth import BrowserAuth, ensure_browser_control_auth
from .handlers import BrowserRouteContext

_log = logging.getLogger("opencomputer.browser_control.server.lifecycle")

LOOPBACK_HOST = "127.0.0.1"


@dataclass(slots=True)
class BrowserServerHandle:
    """Returned from ``start_browser_control_server``. Caller awaits ``stop()``."""

    state: BrowserServerState
    app: Any
    auth: BrowserAuth
    port: int
    driver: ProfileDriver
    server: Any | None = None  # uvicorn.Server when started; None for in-process-only

    async def stop(self) -> None:
        await stop_browser_control_server(self)


async def start_browser_control_server(
    *,
    resolved: ResolvedBrowserConfig,
    driver: ProfileDriver,
    tab_backend: TabOpsBackend,
    ssrf_policy: SsrfPolicy | None = None,
    auth: BrowserAuth | None = None,
    bind_http: bool = True,
    host: str = LOOPBACK_HOST,
    port: int | None = None,
) -> BrowserServerHandle:
    """Build the app + state + (optionally) start uvicorn on 127.0.0.1.

    ``bind_http=False`` is useful for tests that exercise routes via the
    in-process dispatcher only — no socket created.
    """
    if not resolved.enabled:
        raise RuntimeError("browser-control is disabled in config")

    if auth is None:
        auth = await ensure_browser_control_auth()

    state = BrowserServerState(resolved=resolved, port=int(port or resolved.control_port))

    ctx = BrowserRouteContext(
        state=state,
        auth=auth,
        driver=driver,
        tab_backend=tab_backend,
        ssrf_policy=ssrf_policy or resolved.ssrf_policy,
    )
    app = create_app(ctx)

    server: Any | None = None
    if bind_http:
        if host != LOOPBACK_HOST:
            raise ValueError(
                f"server must bind to {LOOPBACK_HOST!r} only; got {host!r}"
            )
        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("uvicorn is required for HTTP bind") from exc
        config = uvicorn.Config(
            app=app,
            host=host,
            port=state.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        # Run uvicorn as a background task so caller stays in the event loop.
        # ``serve()`` returns once ``server.should_exit = True``.
        asyncio.create_task(server.serve())
        # Wait for `started` flag (uvicorn sets this once listening).
        for _ in range(200):
            if getattr(server, "started", False):
                break
            await asyncio.sleep(0.025)
        if not getattr(server, "started", False):
            # Failure to start within 5s — try to stop and raise.
            server.should_exit = True
            raise RuntimeError("uvicorn failed to start within 5s")
        # Resolve actual port (port=0 → kernel-assigned).
        for s in server.servers:
            for sock in s.sockets:
                addr = sock.getsockname()
                if addr and addr[0] == host:
                    state.port = int(addr[1])
                    break
    state.server = server

    return BrowserServerHandle(
        state=state, app=app, auth=auth, port=state.port, driver=driver, server=server
    )


async def stop_browser_control_server(handle: BrowserServerHandle) -> None:
    """Reverse-order shutdown — best-effort, swallow per-step errors."""
    state = handle.state
    driver = handle.driver

    # 1. Tear down active profiles first (so in-flight Playwright commands
    #    fault before uvicorn refuses new requests).
    for runtime in list(state.profiles.values()):
        try:
            await teardown_profile(runtime, driver=driver)
        except Exception as exc:
            _log.debug("teardown_profile raised: %s", exc)
    state.profiles.clear()

    # 2. Stop uvicorn.
    server = handle.server
    if server is not None:
        try:
            server.should_exit = True
            for _ in range(200):
                if not getattr(server, "started", True):
                    break
                # `started` flips False once serve() exits.
                await asyncio.sleep(0.025)
        except Exception as exc:
            _log.debug("uvicorn shutdown raised: %s", exc)

    state.server = None
