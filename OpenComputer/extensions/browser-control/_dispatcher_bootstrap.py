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

Wave-3.2 — CDP tab openers
--------------------------
W3 shipped the ``ProfileDriver`` interface and per-capability callable
slots but did NOT wire production callables for the local-managed
(openclaw) tab opener / focuser / closer paths. This module now wires:

  * ``connect_managed`` — given the launched ``RunningChrome``, attach
    Playwright via ``connect_browser`` and wrap as a
    ``PlaywrightSession`` so ``runtime.playwright_session`` becomes
    populated for downstream agent handlers (snapshot / screenshot /
    navigate by target_id).
  * ``open_tab_via_cdp`` — ``new_page()`` + ``page.goto(url)`` (the
    navigation guard is reused from the agent handlers via the
    pre-nav SSRF check) and returns ``TabInfo(target_id, url, title)``.
  * ``focus_tab_via_cdp`` — looks up the page via
    ``PlaywrightSession.get_page_for_target`` and calls
    ``page.bring_to_front()``.
  * ``close_tab_via_cdp`` — looks up the page and calls ``page.close()``.

Remote-CDP wiring (``connect_remote`` / ``disconnect_remote``) remains
out of scope; persistent-Playwright tab ops likewise stay deferred.

Per-profile cache
-----------------
We cache the ``RunningChrome`` + ``ConnectedBrowser`` + ``PlaywrightSession``
keyed by profile name on a module-level dict. The cache is consulted by
``connect_managed`` so a second call doesn't re-launch Chrome; it's also
consulted by ``page_resolver`` (wired into ``ctx.extra``) so the agent
handler page-resolution path uses the same session the openers populated.

# TODO(wave-3.3): cleanup cache on plugin teardown — there is no clean
# shutdown story yet for in-process plugins. The cache leaks the
# RunningChrome subprocess + the Playwright Browser handle on process
# exit, which is acceptable since the OS reaps them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("opencomputer.browser_control.dispatcher_bootstrap")

# Single-flight init lock. Module-level so concurrent ``Browser.execute``
# calls in the same process share it.
_init_lock = asyncio.Lock()


# ─── per-profile managed-Chrome cache ────────────────────────────────


@dataclass(slots=True)
class _ManagedProfileEntry:
    """Cached state for a single managed (openclaw) profile.

    Populated lazily on the first ``connect_managed`` call for a given
    profile name, then re-used for the lifetime of the Python process.
    """

    running: Any  # RunningChrome
    connected: Any  # ConnectedBrowser
    session: Any  # PlaywrightSession


_managed_cache: dict[str, _ManagedProfileEntry] = {}
_managed_cache_lock = asyncio.Lock()


# Liveness probe lives in chrome/lifecycle so server_context can use the
# same helper without crossing into the dispatcher layer. Re-export the
# private alias for backward-compat with W3.3's tests.
from .chrome.lifecycle import is_running_alive as _is_running_alive  # noqa: E402, I001


async def _close_managed_entry_best_effort(entry: _ManagedProfileEntry) -> None:
    """Best-effort teardown of a popped cache entry's Playwright handles.

    Uses ``force_disconnect_playwright_for_target`` to evict the CDP-
    level cache (``session.cdp._cached_by_cdp_url``) too — without that,
    the next ``connect_browser`` would return the stale handle. All
    failures are swallowed.
    """
    cdp_url = getattr(entry.running, "cdp_url", None) if entry.running is not None else None
    if cdp_url:
        try:
            from extensions.browser_control.session.cdp import (  # type: ignore[import-not-found]
                force_disconnect_playwright_for_target,
            )

            await force_disconnect_playwright_for_target(cdp_url)
        except Exception as exc:  # noqa: BLE001
            _log.debug("evict: force_disconnect raised: %s", exc)
    # Fall back to closing the Browser handle directly in case the CDP-
    # cache eviction missed it (e.g. the entry's connected.browser is a
    # different object than what's cached by URL).
    browser = getattr(entry.connected, "browser", None) if entry.connected is not None else None
    close = getattr(browser, "close", None) if browser is not None else None
    if callable(close):
        try:
            result = close()
            if asyncio.iscoroutine(result):
                # Fire-and-forget — never await a possibly-hung close on
                # a dead WS. The CDP-level eviction above already
                # ensures the next connect makes a fresh connection.
                async def _swallow() -> None:
                    try:
                        await result
                    except Exception as exc:  # noqa: BLE001
                        _log.debug("evict: browser.close raised: %s", exc)

                try:
                    asyncio.create_task(_swallow())
                except RuntimeError:
                    pass
        except Exception as exc:  # noqa: BLE001
            _log.debug("evict: browser.close call raised: %s", exc)


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
    # (enabled=True, default_profile='opencomputer', user-profile present).
    # TODO(wave-3.3): plumb the active profile's `browser:` section in
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
    # Wire a page_resolver into ctx.extra so the agent handlers
    # (navigate / snapshot / screenshot / act / ...) resolve pages off
    # the same PlaywrightSession the openers populated.
    _wire_page_resolver(handle.app)
    return handle.app


def _wire_page_resolver(app: Any) -> None:
    """Stash a page_resolver on the BrowserRouteContext.extra dict.

    The agent handlers consult ``ctx.extra["page_resolver"]`` first; if
    absent they fall back to walking ``runtime.playwright_session``
    directly. Wiring the resolver here means navigate-by-target-id keeps
    working after openers create new pages; without it the fallback
    still works because we populate ``runtime.playwright_session`` in
    ``connect_managed``.
    """
    ctx = getattr(app.state, "browser_ctx", None)
    if ctx is None:
        return

    async def _resolver(runtime: Any, target_id: str) -> Any:
        sess = runtime.playwright_session
        if sess is None:
            raise RuntimeError(
                f"profile {runtime.profile.name!r} has no PlaywrightSession; "
                "connect_managed was not called"
            )
        return await sess.get_page_for_target(target_id)

    ctx.extra["page_resolver"] = _resolver


# ─── managed-Chrome connect / cache ──────────────────────────────────


async def _connect_managed_cached(profile: Any, running: Any) -> Any:
    """Attach Playwright to a launched Chrome and cache the result.

    Returns a ``PlaywrightSession`` ready for the agent handler page-
    resolution path. The cache key is the profile name; subsequent
    calls for the same profile return the cached session unless

      * the underlying ``RunningChrome`` identity differs (a fresh
        launch happened upstream), or
      * the cached entry's Chrome subprocess is no longer alive
        (out-of-band death — see ``_is_running_alive``).

    Wave 3.3 — without the liveness probe, a cached entry pointing at
    a dead Chrome would be returned to callers that then hang on the
    dead CDP WebSocket. We evict + re-attach in that case.
    """
    from extensions.browser_control.session import (  # type: ignore[import-not-found]
        PlaywrightSession,
        connect_browser,
    )

    profile_name = profile.name
    stale_entry: _ManagedProfileEntry | None = None
    async with _managed_cache_lock:
        cached = _managed_cache.get(profile_name)
        if cached is not None:
            # Liveness gate: only honour the cache if the cached Chrome
            # process is the one the caller passed AND it is still
            # alive. Otherwise evict so we re-attach against the fresh
            # ``running``.
            if cached.running is running and _is_running_alive(cached.running):
                return cached.session
            # Stale — drop the entry under the lock; close the
            # Playwright handles outside the lock so a slow close()
            # can't pin the cache lock for other callers.
            _managed_cache.pop(profile_name, None)
            stale_entry = cached

    if stale_entry is not None:
        await _close_managed_entry_best_effort(stale_entry)

    async with _managed_cache_lock:
        # Re-check under the lock — another coroutine that raced us to
        # the eviction may have already re-populated the cache for the
        # same fresh ``running``.
        cached = _managed_cache.get(profile_name)
        if cached is not None and cached.running is running and _is_running_alive(cached.running):
            return cached.session

        connected = await connect_browser(running.cdp_url)
        session = PlaywrightSession(browser=connected.browser, cdp_url=running.cdp_url)
        _managed_cache[profile_name] = _ManagedProfileEntry(
            running=running,
            connected=connected,
            session=session,
        )
        _log.debug(
            "connect_managed: profile=%s pid=%s cdp=%s — Playwright attached",
            profile_name,
            getattr(running, "pid", None),
            running.cdp_url,
        )
        return session


def _build_default_profile_driver() -> Any:
    """Wire the openclaw + chrome-mcp driver paths.

    ``connect_managed`` attaches Playwright to the launched Chrome and
    caches the resulting session per profile. ``connect_remote`` /
    ``disconnect_remote`` remain ``None`` for now — no production caller
    exercises those paths in W3.x; the remote-CDP path lands later.
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

    async def _launch_managed(profile: ResolvedBrowserProfile) -> Any:
        # Re-resolve a default config — cheap, idempotent, no I/O.
        from extensions.browser_control.profiles.resolver import (  # type: ignore[import-not-found]
            resolve_browser_config,
        )

        # Short-circuit: if we already cached a RunningChrome for this
        # profile AND its subprocess is still alive, return it instead
        # of relaunching.
        #
        # Wave 3.3 — if the proc is dead (out-of-band kill, crash) we
        # MUST evict the stale cache entry (and close its Playwright
        # handles best-effort) so a subsequent ``_connect_managed``
        # builds a fresh session against the relaunched Chrome rather
        # than handing back the dead WS handle.
        stale_entry: _ManagedProfileEntry | None = None
        async with _managed_cache_lock:
            cached = _managed_cache.get(profile.name)
            if cached is not None and cached.running is not None:
                if _is_running_alive(cached.running):
                    return cached.running
                _managed_cache.pop(profile.name, None)
                stale_entry = cached

        if stale_entry is not None:
            await _close_managed_entry_best_effort(stale_entry)

        resolved_local = resolve_browser_config({"enabled": True}, {})
        return await launch_openclaw_chrome(resolved_local, profile)

    async def _connect_managed(profile: ResolvedBrowserProfile, running: Any) -> Any:
        return await _connect_managed_cached(profile, running)

    async def _stop_managed(running: Any) -> None:
        await stop_openclaw_chrome(running)

    async def _spawn_chrome_mcp(profile: ResolvedBrowserProfile) -> Any:
        # spawn_chrome_mcp is keyword-only — passing the profile positionally
        # crashes with "takes 0 positional arguments but 1 was given". Extract
        # the relevant fields and pass as kwargs. (Wave 4 hotfix — surfaced
        # when the agent first exercised the existing-session/user profile.)
        return await spawn_chrome_mcp(
            profile_name=profile.name,
            user_data_dir=str(profile.user_data_dir) if profile.user_data_dir else None,
        )

    async def _close_chrome_mcp(client: Any) -> None:
        close = getattr(client, "close", None) or getattr(client, "aclose", None)
        if close is None:
            return
        result = close()
        if asyncio.iscoroutine(result):
            await result

    return ProfileDriver(
        launch_managed=_launch_managed,
        connect_managed=_connect_managed,
        stop_managed=_stop_managed,
        spawn_chrome_mcp=_spawn_chrome_mcp,
        close_chrome_mcp=_close_chrome_mcp,
        connect_remote=None,  # TODO(wave-3.3): remote-CDP wiring
        disconnect_remote=None,
    )


def _build_default_tab_ops_backend() -> Any:
    """Tab-ops backend wiring both the local-managed and chrome-mcp paths.

    Local-managed (openclaw, Playwright/CDP):
      * ``list_tabs(runtime)`` — walks the cached PlaywrightSession.
      * ``open_tab_via_cdp(runtime, url)`` — ``new_page()`` + ``goto``.
      * ``focus_tab_via_cdp(runtime, target_id)`` — ``bring_to_front()``.
      * ``close_tab_via_cdp(runtime, target_id)`` — ``page.close()``.

    Chrome MCP (existing-session / user profile, v0.5 Bug B):
      * ``open_tab_via_mcp(runtime, url)`` — ``new_page`` MCP tool.
      * ``focus_tab_via_mcp(runtime, target_id)`` — ``select_page``
        with ``bringToFront: true``.
      * ``close_tab_via_mcp(runtime, target_id)`` — ``close_page``.

    The MCP tool names are confirmed via the upstream
    ``chrome-devtools-mcp`` server's ``list_tools()`` response (see
    ``docs/refs/openclaw/browser/04-ai-and-snapshot.md`` — the OpenClaw
    integration table). Args use ``pageId: number``; we convert from
    OpenComputer's string ``target_id`` via ``int()``.

    Persistent-Playwright variants stay ``None`` for now —
    remote-CDP opener wiring lands later.
    """
    from extensions.browser_control.server_context import (  # type: ignore[import-not-found]
        ProfileRuntimeState,
        TabInfo,
    )
    from extensions.browser_control.server_context.tab_ops import (  # type: ignore[import-not-found]
        TabOpsBackend,
    )
    from extensions.browser_control.session.target_id import (  # type: ignore[import-not-found]
        page_target_id,
    )

    async def _list_tabs(runtime: ProfileRuntimeState) -> list[TabInfo]:
        sess = runtime.playwright_session
        if sess is None:
            return []
        out: list[TabInfo] = []
        for page in sess.list_pages():
            try:
                tid = await page_target_id(page, cdp_url=sess.cdp_url)
            except Exception:  # noqa: BLE001
                continue
            if not tid:
                continue
            try:
                title = await page.title()
            except Exception:  # noqa: BLE001
                title = ""
            url = getattr(page, "url", "") or ""
            out.append(TabInfo(target_id=tid, url=url, title=title))
        return out

    async def _new_page_for_session(sess: Any) -> Any:
        """Pick a context off the connected browser (or create one) and
        return a fresh Page with the navigation guard pre-installed.
        """
        contexts = list(getattr(sess.browser, "contexts", []) or [])
        if contexts:
            ctx = contexts[0]
        else:
            ctx = await sess.browser.new_context()
        return await ctx.new_page()

    async def _open_tab_via_cdp(
        runtime: ProfileRuntimeState, url: str
    ) -> TabInfo:
        sess = runtime.playwright_session
        if sess is None:
            raise RuntimeError(
                f"open_tab: profile {runtime.profile.name!r} has no PlaywrightSession; "
                "ensure_profile_running was not called"
            )

        page = await _new_page_for_session(sess)

        # Navigate. Any SSRF policy is enforced by the navigate route's
        # pre-nav check; the openers themselves trust the caller for
        # the simple "open and goto" path. The 20s default mirrors the
        # navigate handler.
        try:
            await page.goto(url, timeout=20_000)
        except Exception as exc:
            # Best-effort: close the half-opened page so we don't leak
            # an empty tab.
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"open_tab: navigation to {url!r} failed: {exc}") from exc

        tid = await page_target_id(page, cdp_url=sess.cdp_url)
        if not tid:
            raise RuntimeError(
                f"open_tab: could not resolve target_id for new page at {url!r}"
            )

        try:
            title = await page.title()
        except Exception:  # noqa: BLE001
            title = ""
        final_url = getattr(page, "url", url) or url
        return TabInfo(target_id=tid, url=final_url, title=title)

    async def _focus_tab_via_cdp(
        runtime: ProfileRuntimeState, target_id: str
    ) -> None:
        sess = runtime.playwright_session
        if sess is None:
            raise RuntimeError(
                f"focus_tab: profile {runtime.profile.name!r} has no PlaywrightSession"
            )
        page = await sess.get_page_for_target(target_id)
        await page.bring_to_front()

    async def _close_tab_via_cdp(
        runtime: ProfileRuntimeState, target_id: str
    ) -> None:
        sess = runtime.playwright_session
        if sess is None:
            raise RuntimeError(
                f"close_tab: profile {runtime.profile.name!r} has no PlaywrightSession"
            )
        try:
            page = await sess.get_page_for_target(target_id)
        except LookupError:
            # Already closed — idempotent.
            return
        await page.close()

    # ─── chrome-mcp tab ops (v0.5 Bug B) ──────────────────────────

    def _require_mcp_client(runtime: ProfileRuntimeState, verb: str) -> Any:
        client = runtime.chrome_mcp_client
        if client is None:
            raise RuntimeError(
                f"{verb}: profile {runtime.profile.name!r} has no Chrome MCP client; "
                "ensure_profile_running was not called or spawn_chrome_mcp failed"
            )
        return client

    def _page_id_to_target_id(page_id: Any) -> str:
        # Chrome MCP stores ids as numbers; OpenComputer's TabInfo uses
        # strings. Round-trip cleanly to keep selection/state stable.
        return str(int(page_id)) if page_id is not None else ""

    def _target_id_to_page_id(target_id: str, *, verb: str) -> int:
        try:
            return int(target_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{verb}: target_id {target_id!r} is not a Chrome-MCP pageId"
            ) from exc

    async def _list_pages_via_mcp(client: Any) -> list[dict[str, Any]]:
        """Call ``list_pages`` and normalise the structured result.

        chrome-devtools-mcp returns
        ``structuredContent: {pages: [{id, url, selected?}, ...]}``
        when launched with ``--experimentalStructuredContent`` (the
        default flag set we ship). We tolerate the legacy shape too.
        """
        result = await client.call_tool("list_pages", {})
        sc = result.structured_content or {}
        pages = sc.get("pages") if isinstance(sc, dict) else None
        if not isinstance(pages, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in pages:
            if isinstance(entry, dict) and "id" in entry:
                out.append(entry)
        return out

    async def _list_tabs_via_mcp(runtime: ProfileRuntimeState) -> list[TabInfo]:
        client = runtime.chrome_mcp_client
        if client is None:
            return []
        pages = await _list_pages_via_mcp(client)
        out: list[TabInfo] = []
        for entry in pages:
            tid = _page_id_to_target_id(entry.get("id"))
            if not tid:
                continue
            url = entry.get("url") or ""
            # chrome-devtools-mcp doesn't surface titles in list_pages;
            # leave blank — matches OpenClaw's adapter.
            out.append(
                TabInfo(
                    target_id=tid,
                    url=str(url),
                    title="",
                    type="page",
                    selected=bool(entry.get("selected", False)),
                )
            )
        return out

    async def _open_tab_via_chrome_mcp(
        runtime: ProfileRuntimeState, url: str
    ) -> TabInfo:
        client = _require_mcp_client(runtime, "open_tab")
        # ``new_page`` opens a tab and selects it; the response carries
        # a ``pages`` list and a ``selected`` page id so we can stamp
        # ``last_target_id``. Some server versions echo only the new
        # page; we handle both shapes.
        result = await client.call_tool("new_page", {"url": url})
        sc = result.structured_content or {}
        target_id = ""
        url_back = url
        pages = sc.get("pages") if isinstance(sc, dict) else None
        if isinstance(pages, list):
            # Prefer the explicitly-selected page; fall back to last.
            chosen: dict[str, Any] | None = None
            for entry in pages:
                if isinstance(entry, dict) and entry.get("selected"):
                    chosen = entry
                    break
            if chosen is None and pages:
                last = pages[-1]
                if isinstance(last, dict):
                    chosen = last
            if chosen is not None:
                target_id = _page_id_to_target_id(chosen.get("id"))
                url_back = str(chosen.get("url") or url)
        if not target_id and isinstance(sc, dict) and "id" in sc:
            target_id = _page_id_to_target_id(sc.get("id"))
            url_back = str(sc.get("url") or url)
        if not target_id:
            # Fall back to a list_pages re-read — the MCP server always
            # tracks the live page set even if the new_page response is
            # sparse on older builds.
            pages = await _list_pages_via_mcp(client)
            if pages:
                target_id = _page_id_to_target_id(pages[-1].get("id"))
                url_back = str(pages[-1].get("url") or url)
        if not target_id:
            raise RuntimeError(
                f"open_tab: chrome-mcp new_page did not return a usable pageId "
                f"for url={url!r}"
            )
        return TabInfo(
            target_id=target_id,
            url=url_back,
            title="",
            type="page",
            selected=True,
        )

    async def _focus_tab_via_chrome_mcp(
        runtime: ProfileRuntimeState, target_id: str
    ) -> None:
        client = _require_mcp_client(runtime, "focus_tab")
        page_id = _target_id_to_page_id(target_id, verb="focus_tab")
        await client.call_tool(
            "select_page", {"pageId": page_id, "bringToFront": True}
        )

    async def _close_tab_via_chrome_mcp(
        runtime: ProfileRuntimeState, target_id: str
    ) -> None:
        client = _require_mcp_client(runtime, "close_tab")
        page_id = _target_id_to_page_id(target_id, verb="close_tab")
        try:
            await client.call_tool("close_page", {"pageId": page_id})
        except Exception as exc:  # noqa: BLE001
            # close_page can return a tool-error if the page is already
            # gone — keep close_tab idempotent.
            from extensions.browser_control.snapshot.chrome_mcp import (  # type: ignore[import-not-found]
                ChromeMcpToolError,
            )

            if isinstance(exc, ChromeMcpToolError):
                _log.debug(
                    "close_tab: chrome-mcp returned tool error for pageId=%s — "
                    "treating as already-closed: %s",
                    page_id,
                    exc,
                )
                return
            raise

    # The ``list_tabs`` callable on the backend has to dispatch by
    # capability too — for chrome-mcp profiles, walk the MCP server's
    # page list; for openclaw, walk the PlaywrightSession.
    async def _list_tabs_dispatched(runtime: ProfileRuntimeState) -> list[TabInfo]:
        from extensions.browser_control.profiles.capabilities import (  # type: ignore[import-not-found]
            get_browser_profile_capabilities,
        )

        capabilities = get_browser_profile_capabilities(runtime.profile)
        if capabilities.uses_chrome_mcp:
            return await _list_tabs_via_mcp(runtime)
        return await _list_tabs(runtime)

    return TabOpsBackend(
        list_tabs=_list_tabs_dispatched,
        open_tab_via_cdp=_open_tab_via_cdp,
        focus_tab_via_cdp=_focus_tab_via_cdp,
        close_tab_via_cdp=_close_tab_via_cdp,
        open_tab_via_mcp=_open_tab_via_chrome_mcp,
        focus_tab_via_mcp=_focus_tab_via_chrome_mcp,
        close_tab_via_mcp=_close_tab_via_chrome_mcp,
    )


def reset_for_tests() -> None:
    """Test helper — clears the dispatcher slot and recreates the lock.

    Lets tests isolate the lazy-init pathway without tripping over a
    process-global app set by an earlier test. Also clears the per-
    profile managed-Chrome cache so tests don't observe leftover
    sessions from a previous test's mocks.
    """
    global _init_lock, _managed_cache_lock
    from extensions.browser_control.client.fetch import (  # type: ignore[import-not-found]
        set_default_dispatcher_app,
    )

    set_default_dispatcher_app(None)
    _init_lock = asyncio.Lock()
    _managed_cache.clear()
    _managed_cache_lock = asyncio.Lock()


__all__ = [
    "ensure_dispatcher_app_ready",
    "reset_for_tests",
]
