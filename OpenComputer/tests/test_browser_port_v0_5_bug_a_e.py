"""Regression tests for browser-port v0.5 — Bug A + Bug E.

Bug A — Adapter ctx skips dispatcher lazy-bootstrap
---------------------------------------------------
Pre-fix: ``Browser.execute()`` lazy-built the in-process dispatcher app
via ``_ensure_dispatcher_ready_or_raise``. Adapter ctx imports
``fetch_browser_json`` directly through ``BrowserActions``, never going
through ``Browser.execute()``, so the bootstrap never fired and the
first ``ctx.fetch_in_page(...)`` failed with
``BrowserServiceError("In-process dispatcher is not registered ...")``.

Post-fix: the lazy bootstrap lives inside ``client/fetch.py``
``fetch_browser_json`` itself — every caller (Browser tool, adapter
ctx, future direct callers) gets it transparently.

Bug E — HTTP 404/500 for actions whose route handlers aren't installed
---------------------------------------------------------------------
Pre-fix: routes that depend on a Playwright session (``/snapshot``,
``/act``, ...) raised either ``BrowserHandlerError(503,
'no_session')`` or an unhandled ``RuntimeError`` (→ 500) when invoked
on a profile whose driver doesn't carry a session (e.g. chrome-mcp's
``user`` profile). Tab ops on a missing-callable backend raised plain
``RuntimeError`` → uncaught 500.

Post-fix: missing-driver-capability errors raise
``DriverUnsupportedError`` (subclass of ``BrowserHandlerError``) which
``safe_call`` maps to a structured 501 with
``{"error": {"code": "driver_unsupported", "message": ...}}``.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("send2trash")

from extensions.browser_control._dispatcher_bootstrap import (  # noqa: E402
    reset_for_tests,
)
from extensions.browser_control._utils.errors import BrowserServiceError  # noqa: E402
from extensions.browser_control.client.fetch import (  # noqa: E402
    fetch_browser_json,
    get_default_dispatcher_app,
)


@pytest.fixture(autouse=True)
def _reset_dispatcher():
    """Each test starts with a fresh, unbuilt dispatcher slot."""
    reset_for_tests()
    yield
    reset_for_tests()


# ─── Bug A — bootstrap fires from fetch_browser_json ───────────────────


@pytest.mark.asyncio
async def test_fetch_browser_json_lazy_bootstraps_dispatcher():
    """Calling ``fetch_browser_json`` with a path-only URL and no
    ``dispatcher_app`` arg must now lazy-build the dispatcher app
    transparently — no ``Browser.execute()`` precondition.

    The smoke target is ``GET /``, which routes to ``handle_status``
    and returns a JSON status payload.
    """
    assert get_default_dispatcher_app() is None, (
        "test setup: dispatcher slot should start empty"
    )

    body = await fetch_browser_json("GET", "/", timeout=5.0)

    assert get_default_dispatcher_app() is not None, (
        "fetch_browser_json should have lazy-built the dispatcher app"
    )
    assert isinstance(body, dict)
    # handle_status payload includes these keys.
    for k in ("enabled", "profile", "default_profile", "running", "status"):
        assert k in body


@pytest.mark.asyncio
async def test_fetch_browser_json_bootstrap_idempotent_under_concurrency():
    """Two concurrent first-callers on ``fetch_browser_json`` must
    converge to the same app object — the bootstrap helper's single-
    flight lock collapses the race.
    """
    assert get_default_dispatcher_app() is None

    async def _hit():
        return await fetch_browser_json("GET", "/", timeout=5.0)

    results = await asyncio.gather(*[_hit() for _ in range(8)])
    for body in results:
        assert isinstance(body, dict) and "status" in body

    app = get_default_dispatcher_app()
    assert app is not None


@pytest.mark.asyncio
async def test_adapter_ctx_fetch_in_page_does_not_raise_dispatcher_not_registered():
    """End-to-end Bug A regression — an adapter ctx whose first action
    is ``ctx.fetch_in_page(...)`` must NOT trip the legacy
    "In-process dispatcher is not registered" error. The call may fail
    for OTHER reasons (no real Chrome attached) but the failure mode
    must not be the dispatcher-bootstrap one.
    """
    # Build a minimal AdapterContext without going through Browser tool.
    from pathlib import Path

    from extensions.adapter_runner import Strategy, adapter
    from extensions.adapter_runner._ctx import AdapterContext

    @adapter(
        site="testsite",
        name="probe",
        description="bug-A regression probe",
        domain="example.invalid",
        strategy=Strategy.COOKIE,
        browser=True,
    )
    async def run(args, ctx):  # noqa: ARG001 — never actually invoked
        return []

    spec = run._adapter_spec  # type: ignore[attr-defined]

    ctx = AdapterContext.create(
        spec=spec,
        profile_home=Path("/tmp/oc-bug-a-probe"),
    )

    # Sanity: dispatcher slot starts empty (autouse fixture reset it).
    assert get_default_dispatcher_app() is None

    # Make ``BrowserActions.browser_act`` -> dispatcher path. The actual
    # request will fail (no real Chrome), but it must NOT fail with the
    # legacy "dispatcher not registered" error.
    try:
        await ctx.fetch_in_page("https://example.invalid/probe")
    except BrowserServiceError as exc:
        assert "In-process dispatcher is not registered" not in str(exc), (
            f"Bug A regression — adapter ctx still hit the legacy bootstrap "
            f"error: {exc}"
        )
    except Exception:
        # Any other failure (no real Chrome, no real session) is fine —
        # the only thing we're guarding against is the legacy bootstrap
        # failure.
        pass

    # Either way, the bootstrap should have fired transparently.
    assert get_default_dispatcher_app() is not None, (
        "Bug A regression — fetch_browser_json did not lazy-build the "
        "dispatcher app for the adapter-ctx path"
    )

    # Cleanup the registry entry our @adapter decorator created.
    from extensions.adapter_runner import clear_registry_for_tests

    clear_registry_for_tests()


# ─── Bug E — DriverUnsupportedError → structured 501 ───────────────────


@pytest.mark.asyncio
async def test_tab_op_missing_driver_callable_raises_driver_unsupported():
    """tab_ops._pick_*_callable now raises ``DriverUnsupportedError``
    (instead of plain ``RuntimeError``) when the active driver doesn't
    have the required callable. Verifies the typed error carries the
    501 status, ``driver_unsupported`` code, and identifies the action,
    driver, and profile.
    """
    from extensions.browser_control.profiles import (
        resolve_browser_config,
        resolve_profile,
    )
    from extensions.browser_control.server.handlers import DriverUnsupportedError
    from extensions.browser_control.server_context import (
        ProfileRuntimeState,
        TabInfo,
        open_tab,
    )
    from extensions.browser_control.server_context.tab_ops import TabOpsBackend

    cfg = resolve_browser_config({})
    profile = resolve_profile(cfg, "user")  # chrome-mcp profile
    assert profile is not None
    runtime = ProfileRuntimeState(profile=profile)

    async def _list_tabs(_r) -> list[TabInfo]:
        return []

    # No open_tab_via_mcp supplied → driver_unsupported.
    backend = TabOpsBackend(list_tabs=_list_tabs)

    with pytest.raises(DriverUnsupportedError) as exc_info:
        await open_tab(runtime, "https://x/", backend=backend)

    err = exc_info.value
    assert err.status == 501
    assert err.code == "driver_unsupported"
    assert err.action == "open_tab"
    assert err.profile == "user"
    assert err.driver == "local-existing-session"
    # Subclass of BrowserHandlerError so safe_call maps it correctly.
    from extensions.browser_control.server.handlers import BrowserHandlerError

    assert isinstance(err, BrowserHandlerError)


@pytest.mark.asyncio
async def test_chrome_mcp_profile_action_returns_501_via_dispatcher():
    """Full route → safe_call → JSONResponse path: a profile whose
    driver lacks the requested capability returns a structured 501
    ``driver_unsupported`` body, NOT a bare 404 / 500.

    We build a synthetic FastAPI app with a chrome-mcp profile but a
    ``ProfileDriver`` whose ``spawn_chrome_mcp`` IS wired (so bring-up
    succeeds), paired with a ``TabOpsBackend`` whose ``open_tab_via_mcp``
    is None — exercising the ``_pick_open_callable`` 501 path through
    the real dispatcher pipeline.
    """
    from extensions.browser_control.profiles import (
        resolve_browser_config,
    )
    from extensions.browser_control.server import (
        BrowserAuth,
        create_app,
    )
    from extensions.browser_control.server.dispatcher import (
        dispatch_browser_control_request,
    )
    from extensions.browser_control.server.handlers import BrowserRouteContext
    from extensions.browser_control.server_context import (
        BrowserServerState,
        ProfileDriver,
        TabInfo,
    )
    from extensions.browser_control.server_context.tab_ops import TabOpsBackend

    cfg = resolve_browser_config({"enabled": True})
    state = BrowserServerState(resolved=cfg, port=0)

    # Driver wires only ``spawn_chrome_mcp`` (returns a stand-in client
    # so bring-up doesn't try to launch real npx). The backend has
    # ``list_tabs`` but NO ``open_tab_via_mcp`` — the gap the test
    # exercises.
    async def _spawn_mcp(_profile):
        class _StubMcpClient:
            pass

        return _StubMcpClient()

    async def _close_mcp(_client):
        return None

    driver = ProfileDriver(
        spawn_chrome_mcp=_spawn_mcp,
        close_chrome_mcp=_close_mcp,
    )

    async def _list_tabs(_runtime) -> list[TabInfo]:
        return []

    backend = TabOpsBackend(list_tabs=_list_tabs)

    ctx = BrowserRouteContext(
        state=state,
        auth=BrowserAuth(),
        driver=driver,
        tab_backend=backend,
    )
    app = create_app(ctx)

    result = await dispatch_browser_control_request(
        app,
        method="POST",
        path="/tabs/open",
        body={"profile": "user", "url": "https://example.com/"},
    )
    assert result.status == 501, (
        f"expected 501 driver_unsupported, got {result.status}: "
        f"body={result.body!r}"
    )
    body = result.body if isinstance(result.body, dict) else {}
    err = body.get("error", {})
    assert err.get("code") == "driver_unsupported", (
        f"expected code 'driver_unsupported', got body={body!r}"
    )
    assert "user" in str(err.get("message", ""))


@pytest.mark.asyncio
async def test_lifecycle_missing_managed_driver_raises_driver_unsupported():
    """``ensure_profile_running`` for a local-managed profile whose
    driver lacks ``launch_managed`` now raises ``DriverUnsupportedError``
    — still a ``RuntimeError`` subclass for backward compat with tests
    that assert ``pytest.raises(RuntimeError, ...)``, but exposes the
    501 ``driver_unsupported`` shape when bubbled through a route.
    """
    from extensions.browser_control.profiles import (
        resolve_browser_config,
    )
    from extensions.browser_control.server.handlers import DriverUnsupportedError
    from extensions.browser_control.server_context import (
        BrowserServerState,
        ProfileDriver,
        ensure_profile_running,
    )

    cfg = resolve_browser_config({"enabled": True})
    state = BrowserServerState(resolved=cfg, port=0)
    driver = ProfileDriver()  # no launch_managed

    with pytest.raises(DriverUnsupportedError) as exc_info:
        await ensure_profile_running(state, "openclaw", driver=driver)

    err = exc_info.value
    assert err.status == 501
    assert err.code == "driver_unsupported"
    assert err.profile == "openclaw"
    # Still a RuntimeError subclass so ``pytest.raises(RuntimeError, ...)``
    # in legacy tests keeps matching.
    assert isinstance(err, RuntimeError)
