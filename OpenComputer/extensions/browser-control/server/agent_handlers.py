"""Agent route handlers — navigate / snapshot / screenshot / pdf / act /
hooks (dialog & file-chooser) / response body / download.

Each handler:

  1. Resolves the profile name (query → body → default).
  2. Ensures the profile is running (lifecycle).
  3. Resolves the page from the runtime's PlaywrightSession +
     ``select_target_id`` fallback chain.
  4. Calls into ``tools_core``.

The page-resolution path is a callable on ``BrowserRouteContext.extra``
(``page_resolver``) so tests don't need a real Playwright Page.

Snapshot and screenshot routes are mounted as POST in this port (vs
OpenClaw which mixed GET/POST) because POST is the only mutating-but-
side-effect-free verb that survives CSRF cleanly. GET semantics on a
snapshot whose result depends on a server-side action timestamp would
be misleading anyway.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..profiles.capabilities import get_browser_profile_capabilities
from ..server_context import select_target_id
from ..tools_core import (
    EvaluateDisabledError,
    arm_dialog,
    arm_file_chooser,
    await_and_save_download,
    execute_single_action,
    is_act_kind,
    read_response_body,
    record_action,
    snapshot_role_via_playwright,
)
from ..tools_core.refs import ref_locator as _ref_locator
from .handlers import (
    BrowserHandlerError,
    BrowserRouteContext,
    DriverUnsupportedError,
    _ensure_running,
    resolve_profile_name,
)

_log = logging.getLogger("opencomputer.browser_control.server.agent_handlers")


# ─── helpers ─────────────────────────────────────────────────────────


PageResolver = Callable[[Any, str], Awaitable[Any]]


async def _resolve_page_once(
    ctx: BrowserRouteContext, *, profile: str | None, target_id: str | None
) -> tuple[Any, str, Any]:
    name = resolve_profile_name(ctx, query_profile=profile, body_profile=profile)
    runtime = await _ensure_running(ctx, name)

    tab_backend = ctx.tab_backend
    tabs = await tab_backend.list_tabs(runtime)

    try:
        chosen = select_target_id(runtime, tabs=tabs, requested=target_id)
    except Exception as exc:
        raise BrowserHandlerError(
            f"could not resolve target_id: {exc}", status=404, code="tab_not_found"
        ) from exc

    page_resolver: PageResolver | None = ctx.extra.get("page_resolver")
    if page_resolver is None:
        # Fallback: walk the playwright_session.
        sess = runtime.playwright_session
        if sess is None:
            # Bug E — chrome-mcp / not-yet-attached profiles don't carry a
            # PlaywrightSession; surface a structured 501 so the agent
            # gets ``driver_unsupported`` rather than an opaque 503.
            capabilities = get_browser_profile_capabilities(runtime.profile)
            if capabilities.uses_chrome_mcp:
                raise DriverUnsupportedError(
                    action="page-level",
                    driver=capabilities.mode,
                    profile=runtime.profile.name,
                    message=(
                        f"page-level actions require a PlaywrightSession; "
                        f"profile {runtime.profile.name!r} uses driver "
                        f"{capabilities.mode!r} which has no Playwright attach"
                    ),
                )
            raise BrowserHandlerError(
                "profile has no PlaywrightSession; cannot resolve page",
                status=503,
                code="no_session",
            )
        page = await sess.get_page_for_target(chosen)
    else:
        page = await page_resolver(runtime, chosen)
    return runtime, chosen, page


async def _probe_session_alive(runtime: Any) -> bool:
    """Fast WS-liveness probe — returns False iff the cached PlaywrightSession's
    CDP WebSocket has wedged.

    Chrome's HTTP control plane and the Playwright CDP WebSocket are
    independent transports. The HTTP-level ``is_chrome_reachable`` probe
    in lifecycle can return success while the WS itself is dead (macOS
    network/power throttling, idle TCP teardown, BFCache pause).

    Tests an existing Page (no new page created) by sending a trivial
    ``page.evaluate("1")`` with a 2s timeout. If the WS is alive, this
    completes in a few ms. If wedged, we hit the timeout cleanly.
    """
    sess = runtime.playwright_session
    if sess is None:
        return False
    try:
        pages = sess.list_pages()
    except Exception:  # noqa: BLE001
        return False
    if not pages:
        # No pages cached — can't probe. Treat as alive; downstream
        # handlers will fail/retry naturally.
        return True
    probe_page = pages[0]
    try:
        await asyncio.wait_for(probe_page.evaluate("1"), timeout=2.0)
        return True
    except (TimeoutError, asyncio.TimeoutError):
        return False
    except Exception:  # noqa: BLE001
        # Page closed / target detached / browser closed — same outcome:
        # the session is unusable, force a re-attach.
        return False


async def _reattach_session(
    ctx: BrowserRouteContext, runtime: Any
) -> None:
    """Evict cached PlaywrightSession and re-attach to the SAME Chrome.

    Avoids a Chrome relaunch (no ghost windows, no lost cookies) by
    going directly through ``connect_managed`` instead of full
    ``ensure_profile_running``. Required because lifecycle's HTTP-level
    probe will short-circuit ``ensure_profile_running`` even when the WS
    is dead, so we'd never get a fresh session that way.
    """
    from .._dispatcher_bootstrap import (  # type: ignore[import-not-found]
        evict_managed_session,
    )

    profile_name = runtime.profile.name
    try:
        await evict_managed_session(profile_name)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "profile %r: evict_managed_session failed: %s", profile_name, exc
        )

    runtime.playwright_session = None
    if runtime.running is None or ctx.driver.connect_managed is None:
        raise BrowserHandlerError(
            f"profile {profile_name!r}: cannot re-attach (no running Chrome "
            f"or connect_managed driver)",
            status=503,
            code="reattach_failed",
        )
    try:
        runtime.playwright_session = await ctx.driver.connect_managed(
            runtime.profile, runtime.running
        )
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "profile %r: re-attach connect_managed failed: %s",
            profile_name,
            exc,
        )
        raise BrowserHandlerError(
            f"CDP re-attach failed: {exc}", status=503, code="reattach_failed"
        ) from exc


async def _resolve_page(
    ctx: BrowserRouteContext, *, profile: str | None, target_id: str | None
) -> tuple[Any, str, Any]:
    """Resolve (runtime, target_id, page) with stale-WS auto-recovery.

    Probes BEFORE doing any operation that touches the CDP WebSocket
    (list_tabs, get_page_for_target). If the probe times out, the WS
    is dead — evict cache and re-attach against the same Chrome before
    proceeding. Without the pre-probe, ``list_tabs`` itself would hang
    on the wedged ``Target.getTargetInfo`` CDP call before any other
    recovery code could run.
    """
    name = resolve_profile_name(ctx, query_profile=profile, body_profile=profile)
    runtime = await _ensure_running(ctx, name)

    # Skip probe for non-Playwright drivers — they don't have the
    # wedge-prone CDP WebSocket path.
    capabilities = get_browser_profile_capabilities(runtime.profile)
    needs_probe = not (
        capabilities.uses_chrome_mcp or capabilities.uses_control_extension
    )
    if needs_probe and not await _probe_session_alive(runtime):
        _log.warning(
            "profile %r: CDP WebSocket probe failed — evicting cached "
            "PlaywrightSession and re-attaching",
            runtime.profile.name,
        )
        await _reattach_session(ctx, runtime)

    return await _resolve_page_once(ctx, profile=profile, target_id=target_id)


def _record(target_id: str) -> None:
    try:
        record_action(target_id)
    except Exception:
        pass


# ─── routes ──────────────────────────────────────────────────────────


async def handle_navigate(
    ctx: BrowserRouteContext,
    *,
    url: str,
    profile: str | None,
    target_id: str | None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    if not url:
        raise BrowserHandlerError("url is required", status=400, code="url_required")
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )

    # Pre-nav SSRF check.
    from ..session.nav_guard import (
        InvalidBrowserNavigationUrlError,
        NavigationGuardPolicy,
        SsrfBlockedError,
        assert_browser_navigation_allowed,
    )

    policy = NavigationGuardPolicy(ssrf_policy=ctx.ssrf_policy)
    try:
        await assert_browser_navigation_allowed(url, policy=policy)
    except (InvalidBrowserNavigationUrlError, SsrfBlockedError) as exc:
        raise BrowserHandlerError(
            str(exc), status=400, code="nav_blocked"
        ) from exc

    full_timeout = max(1000, min(120_000, int(timeout_ms or 20_000)))

    # Same-URL skip: if the page is already on the target URL, navigating
    # again is a no-op for the user. But Chrome can treat same-URL goto
    # as a "no commit needed" same-document nav and never fire the
    # frameNavigated event Playwright is waiting on, so page.goto hangs
    # to its timeout. Just return the cached state. Adapter callers use
    # this purely as a "warm the page" step; if we're already warm, skip.
    try:
        current_url = page.url or ""
    except Exception:  # noqa: BLE001
        current_url = ""
    if current_url and current_url.rstrip("/") == url.rstrip("/"):
        _log.debug(
            "handle_navigate: skipping goto — already on %s (target=%s)",
            current_url,
            tid,
        )
        _record(tid)
        return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "url": current_url}

    # Two-phase navigate: try with a short timeout first; on failure,
    # force-reattach the Playwright session and retry with the full
    # timeout. This catches the "WS evaluate works but page.goto hangs"
    # case that the upstream probe in _resolve_page can't detect (the
    # CDP connection is alive enough to answer Runtime.evaluate but
    # Page.navigate stalls because the renderer is in macOS App Nap).
    short_timeout = min(full_timeout, 6_000)
    try:
        await page.goto(url, timeout=short_timeout, wait_until="commit")
    except Exception as first_exc:  # noqa: BLE001
        _log.warning(
            "profile %r: page.goto(%s) timed out at %sms; reattaching "
            "session and retrying once with %sms",
            runtime.profile.name,
            url,
            short_timeout,
            full_timeout,
            exc_info=False,
        )
        try:
            await _reattach_session(ctx, runtime)
            # Re-resolve page on the fresh session.
            runtime, tid, page = await _resolve_page_once(
                ctx, profile=profile, target_id=target_id
            )
            await page.goto(url, timeout=full_timeout, wait_until="commit")
        except Exception as exc:
            raise BrowserHandlerError(
                f"navigation failed (after reattach): {exc}",
                status=502,
                code="nav_failed",
            ) from exc

    _record(tid)
    final_url = getattr(page, "url", url)
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "url": final_url}


async def handle_snapshot(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    selector: str | None = None,
    frame_selector: str | None = None,
    mode: str = "role",
    max_chars: int | None = None,
    interactive_only: bool = False,
    compact: bool = False,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        result = await snapshot_role_via_playwright(
            page,
            selector=selector,
            frame_selector=frame_selector,
            mode=mode,  # type: ignore[arg-type]
            max_chars=max_chars,
            interactive_only=interactive_only,
            compact=compact,
        )
    except NotImplementedError as exc:
        raise BrowserHandlerError(
            str(exc), status=501, code="aria_mode_unsupported"
        ) from exc

    # Stash refs on the session for later ref_locator resolution.
    sess = runtime.playwright_session
    if sess is not None and tid:
        try:
            sess.store_role_refs(
                target_id=tid,
                refs=result.refs,
                frame_selector=frame_selector,
                mode=mode,  # type: ignore[arg-type]
            )
        except Exception:
            pass

    _record(tid)
    return {
        "ok": True,
        "profile": runtime.profile.name,
        "target_id": tid,
        "snapshot": result.snapshot,
        "truncated": result.truncated,
        "stats": (
            {
                "lines": result.stats.lines,
                "chars": result.stats.chars,
                "refs": result.stats.refs,
                "interactive": result.stats.interactive,
            }
            if result.stats is not None
            else None
        ),
    }


async def handle_screenshot(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    full_page: bool = False,
    type: str = "png",
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    if type not in ("png", "jpeg"):
        raise BrowserHandlerError(
            f"unknown screenshot type: {type!r}", status=400, code="bad_type"
        )
    try:
        buffer: bytes = await page.screenshot(full_page=full_page, type=type)
    except Exception as exc:
        raise BrowserHandlerError(
            f"screenshot failed: {exc}", status=502, code="screenshot_failed"
        ) from exc
    _record(tid)
    import base64

    return {
        "ok": True,
        "profile": runtime.profile.name,
        "target_id": tid,
        "type": type,
        "bytes_b64": base64.b64encode(buffer).decode("ascii"),
        "size": len(buffer),
    }


async def handle_pdf(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        buffer: bytes = await page.pdf(print_background=True)
    except Exception as exc:
        raise BrowserHandlerError(
            f"pdf failed: {exc}", status=502, code="pdf_failed"
        ) from exc
    _record(tid)
    import base64

    return {
        "ok": True,
        "profile": runtime.profile.name,
        "target_id": tid,
        "bytes_b64": base64.b64encode(buffer).decode("ascii"),
        "size": len(buffer),
    }


async def handle_act(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    kind: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not is_act_kind(kind):
        raise BrowserHandlerError(
            f"unknown act kind: {kind!r}", status=400, code="ACT_INVALID_REQUEST"
        )

    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )

    sess = runtime.playwright_session
    cache_entry = sess.get_role_refs(tid) if sess is not None and tid else None
    ref_resolver = lambda r: _ref_locator(page, r, cache_entry=cache_entry)  # noqa: E731

    try:
        result = await execute_single_action(
            page,
            kind,
            params or {},
            ref_resolver=ref_resolver,
            ssrf_policy=ctx.ssrf_policy,
            evaluate_enabled=ctx.state.resolved.evaluate_enabled,
        )
    except EvaluateDisabledError as exc:
        raise BrowserHandlerError(
            str(exc), status=403, code="ACT_EVALUATE_DISABLED"
        ) from exc
    except ValueError as exc:
        raise BrowserHandlerError(
            str(exc), status=400, code="ACT_INVALID_REQUEST"
        ) from exc

    _record(tid)
    return {
        "ok": True,
        "profile": runtime.profile.name,
        "target_id": tid,
        "kind": kind,
        **result,
        "url": getattr(page, "url", ""),
    }


async def handle_arm_dialog(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    accept: bool,
    prompt_text: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    return await arm_dialog(
        page,
        accept=bool(accept),
        prompt_text=prompt_text,
        timeout_ms=int(timeout_ms or 120_000),
    ) | {"profile": runtime.profile.name, "target_id": tid}


async def handle_arm_file_chooser(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    paths: list[str] | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        result = await arm_file_chooser(
            page,
            paths=paths or [],
            timeout_ms=int(timeout_ms or 120_000),
        )
    except (FileNotFoundError, ValueError) as exc:
        raise BrowserHandlerError(
            str(exc), status=400, code="bad_paths"
        ) from exc
    return result | {"profile": runtime.profile.name, "target_id": tid}


async def handle_response_body(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    url: str,
    timeout_ms: int | None = None,
    max_bytes: int | None = None,
    pattern_mode: str = "substring",
) -> dict[str, Any]:
    if not url:
        raise BrowserHandlerError("url is required", status=400, code="url_required")
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        body = await read_response_body(
            page,
            url_pattern=url,
            pattern_mode=pattern_mode,
            timeout_ms=timeout_ms,
            max_bytes=max_bytes,
        )
    except TimeoutError as exc:
        raise BrowserHandlerError(
            str(exc), status=504, code="timeout"
        ) from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "response": body}


async def handle_download(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    out_dir: str | None = None,
    out_path: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    result = await await_and_save_download(
        page, out_dir=out_dir, out_path=out_path, timeout_ms=int(timeout_ms or 120_000)
    )
    return {
        "ok": True,
        "profile": runtime.profile.name,
        "target_id": tid,
        "download": {
            "url": result.url,
            "suggested_filename": result.suggested_filename,
            "path": result.path,
        },
    }
