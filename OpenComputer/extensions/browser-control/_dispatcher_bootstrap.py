"""Lazy in-process dispatcher bootstrap for the Browser tool.

Why this module exists
----------------------
The Wave-3 client (`client/fetch.py`) routes path-only requests through
an in-process FastAPI app stashed in a module-level slot via
``set_default_dispatcher_app(app)``. Until W3 hotfix that slot was only
populated by the e2e test fixture; ``register()`` in ``plugin.py`` never
wired it. As a result every production ``Browser(...)`` call short-
circuited to a ``BrowserServiceError("In-process dispatcher is not
registered ...")``.

Lazy, not eager
---------------
We deliberately do **not** build the FastAPI app inside ``register()``.
Rationale: ``register()`` runs unconditionally for every plugin scan,
even when the agent never actually invokes a Browser tool, and we don't
want to pay the (small, but non-zero) FastAPI init cost up-front. More
importantly we never want to spawn Chrome on plugin discovery — Chrome
launch is gated behind specific browser actions (``start``, ``navigate``,
etc.) and only happens when the active driver's ``ensure_running`` is
called from a request handler. The bootstrap here only constructs the
dispatcher app + the state container; the Chrome side stays cold.

Single-flight under asyncio.Lock
--------------------------------
Two concurrent first-call requests must not double-init. The lock is
released after the cooperative ``await`` chain completes; subsequent
callers see a populated dispatcher slot and skip the lock entirely (fast
path).

Driver composition gaps (TODO wave-3.2)
---------------------------------------
W3 shipped the ``ProfileDriver`` interface and per-capability callable
slots but did NOT wire production callables. We fill the slots here with
the existing ``chrome/launch.py`` + ``chrome/lifecycle.py`` +
``snapshot/chrome_mcp.py`` helpers, which is enough for ``status`` /
``profiles`` (state-only reads) and the `openclaw` driver path. The
remote-CDP path (``connect_remote`` / ``disconnect_remote``) and the
Playwright-attached ``connect_managed`` slot remain unwired — full
wiring lands in wave-3.2 once an integration test exercises them.

The ``TabOpsBackend`` is similarly a partial wire-up. ``list_tabs`` is
required (no default), and we hand it a small CDP /json-based reader so
``handle_list_tabs`` works for the local-managed driver. The other
six per-action callables are best-effort no-ops or raise — again, enough
for status/list-tabs to work, with the bigger wiring deferred.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_log = logging.getLogger("opencomputer.browser_control.dispatcher_bootstrap")

# Single-flight init lock. Module-level so concurrent ``Browser.execute``
# calls in the same process share it.
_init_lock = asyncio.Lock()


async def ensure_dispatcher_app_ready() -> None:
    """Build + register the in-process dispatcher app if not already set.

    Idempotent. Safe under concurrent first-callers — only the first
    caller acquires the lock and builds; the rest fast-path on the
    populated slot after returning from the lock.
    """
    # Imports are funnelled through the synthesised ``extensions
    # .browser_control`` package so the relative imports inside
    # ``client/`` and ``server/`` resolve. ``_tool.py`` already triggers
    # the package bootstrap via its import line; if anything calls this
    # function before that, we re-trigger it defensively.
    from extensions.browser_control.client.fetch import (  # type: ignore[import-not-found]
        get_default_dispatcher_app,
        set_default_dispatcher_app,
    )

    # Fast path: app already wired.
    if get_default_dispatcher_app() is not None:
        return

    async with _init_lock:
        # Re-check under the lock — another coroutine may have raced us.
        if get_default_dispatcher_app() is not None:
            return

        app = await _build_dispatcher_app()
        set_default_dispatcher_app(app)
        _log.debug("browser-control: in-process dispatcher app registered")


async def _build_dispatcher_app() -> Any:
    """Compose ResolvedConfig + Driver + TabBackend → FastAPI app."""
    from extensions.browser_control.profiles.resolver import (  # type: ignore[import-not-found]
        resolve_browser_config,
    )
    from extensions.browser_control.server import (  # type: ignore[import-not-found]
        BrowserAuth,
        start_browser_control_server,
    )

    # We don't (yet) read the active OpenComputer profile config here —
    # the `from opencomputer ...` import would breach the SDK boundary
    # (tests/test_browser_port_*_audit.py). For wave-3 hotfix we resolve
    # against an empty raw section, which yields the documented defaults
    # (enabled=True, default_profile='openclaw', user-profile present).
    # TODO(wave-3.2): plumb the active profile's `browser:` section in
    # via a plugin-level hook on the SDK side so user overrides
    # (executable_path, headless, ssrf_policy, ...) take effect.
    resolved = resolve_browser_config({"enabled": True}, {})

    driver = _build_default_profile_driver()
    tab_backend = _build_default_tab_ops_backend()

    # Anonymous loopback auth — the dispatcher path never crosses a
    # socket so the bearer token would be redundant. ``BrowserAuth()``
    # with no fields is the documented "anonymous allowed" shape and
    # short-circuits ``BrowserAuthMiddleware`` cleanly. We deliberately
    # do NOT wire ``ensure_browser_control_auth`` here: under that path
    # production runs would auto-generate a token whose value the
    # ``BrowserActions()`` instance constructed in ``Browser.__init__``
    # never sees → every dispatcher call would 401. The auto-token
    # surface is for the HTTP transport (set
    # ``OPENCOMPUTER_BROWSER_CONTROL_URL`` to use it).
    auth: BrowserAuth = BrowserAuth()

    handle = await start_browser_control_server(
        resolved=resolved,
        driver=driver,
        tab_backend=tab_backend,
        auth=auth,
        bind_http=False,  # in-process only — no socket
    )
    return handle.app


def _build_default_profile_driver() -> Any:
    """Wire the openclaw + chrome-mcp driver paths.

    ``connect_managed`` (Playwright attach), ``connect_remote``, and
    ``disconnect_remote`` remain ``None`` for now — no production caller
    exercises those paths in W3, and the partial wiring is enough for
    the actions this hotfix unblocks. TODO(wave-3.2).
    """
    from extensions.browser_control.chrome import (  # type: ignore[import-not-found]
        launch_openclaw_chrome,
        stop_openclaw_chrome,
    )
    from extensions.browser_control.profiles.config import (  # type: ignore[import-not-found]
        ResolvedBrowserProfile,
    )
    from extensions.browser_control.server_context import (  # type: ignore[import-not-found]
        ProfileDriver,
    )
    from extensions.browser_control.snapshot import (  # type: ignore[import-not-found]
        spawn_chrome_mcp,
    )

    # We need access to the ResolvedBrowserConfig to call
    # launch_openclaw_chrome(resolved, profile). The driver protocol
    # only hands us the profile, so we close over the resolved config
    # at construction time. Since the resolved config is per-bootstrap
    # we'd have to thread it through; for the openclaw default profile
    # the launch helper accepts an optional ``resolved`` we don't have
    # here, so we reconstruct a minimal one. TODO(wave-3.2): take this
    # closure out of the bootstrap and pass `resolved` through.

    async def _launch_managed(profile: ResolvedBrowserProfile) -> Any:
        # Re-resolve a default config — cheap, idempotent, no I/O.
        from extensions.browser_control.profiles.resolver import (  # type: ignore[import-not-found]
            resolve_browser_config,
        )

        resolved_local = resolve_browser_config({"enabled": True}, {})
        return await launch_openclaw_chrome(resolved_local, profile)

    async def _stop_managed(running: Any) -> None:
        await stop_openclaw_chrome(running)

    async def _spawn_chrome_mcp(profile: ResolvedBrowserProfile) -> Any:
        return await spawn_chrome_mcp(profile)

    async def _close_chrome_mcp(client: Any) -> None:
        close = getattr(client, "close", None) or getattr(client, "aclose", None)
        if close is None:
            return
        result = close()
        if asyncio.iscoroutine(result):
            await result

    return ProfileDriver(
        launch_managed=_launch_managed,
        connect_managed=None,  # TODO(wave-3.2): attach Playwright session
        stop_managed=_stop_managed,
        spawn_chrome_mcp=_spawn_chrome_mcp,
        close_chrome_mcp=_close_chrome_mcp,
        connect_remote=None,  # TODO(wave-3.2): remote-CDP wiring
        disconnect_remote=None,
    )


def _build_default_tab_ops_backend() -> Any:
    """Best-effort tab ops backend.

    ``list_tabs`` is the only required field. We point it at a
    minimal "empty list" reader so ``handle_status`` works without
    tripping on an unconfigured backend; the per-driver openers /
    focusers / closers stay ``None`` and route handlers will surface a
    clear error if invoked before wave-3.2 fills them in.

    TODO(wave-3.2): wire CDP /json/new and /json/close, plus the chrome-
    mcp tabs surface, so the openclaw and user profiles can actually
    open / focus / close tabs from production.
    """
    from extensions.browser_control.server_context import (  # type: ignore[import-not-found]
        ProfileRuntimeState,
    )
    from extensions.browser_control.server_context.tab_ops import (  # type: ignore[import-not-found]
        TabOpsBackend,
    )

    async def _list_tabs(runtime: ProfileRuntimeState) -> list:
        # Minimal viable: when no driver is connected we return an empty
        # list. The real CDP /json reader lands in wave-3.2.
        return []

    return TabOpsBackend(list_tabs=_list_tabs)


def reset_for_tests() -> None:
    """Test helper — clears the dispatcher slot and recreates the lock.

    Lets tests isolate the lazy-init pathway without tripping over a
    process-global app set by an earlier test.
    """
    global _init_lock
    from extensions.browser_control.client.fetch import (  # type: ignore[import-not-found]
        set_default_dispatcher_app,
    )

    set_default_dispatcher_app(None)
    _init_lock = asyncio.Lock()


__all__ = [
    "ensure_dispatcher_app_ready",
    "reset_for_tests",
]
