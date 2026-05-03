"""Tests for the W3 hotfix — lazy in-process dispatcher bootstrap.

Coverage:
  * First production-shape ``Browser(action='status')`` call wires the
    dispatcher app on its own (no manual ``set_default_dispatcher_app``).
  * Subsequent calls reuse the same app (no rebuild).
  * Concurrent first-call races resolve to a single build (asyncio.Lock
    single-flight).
  * The bootstrap composes a ``ResolvedBrowserConfig`` + ``ProfileDriver``
    + ``TabOpsBackend`` such that the dispatcher pipeline serves
    ``GET /`` (handle_status) without raising — the smoke test the
    orchestrator exercised manually.

The tests skip cleanly if the FastAPI / send2trash optional deps are
absent — the production hotfix only matters in environments that have
the browser-control extras installed.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("send2trash")

from extensions.browser_control._dispatcher_bootstrap import (  # noqa: E402
    ensure_dispatcher_app_ready,
    reset_for_tests,
)
from extensions.browser_control._tool import Browser  # noqa: E402
from extensions.browser_control.client.fetch import (  # noqa: E402
    get_default_dispatcher_app,
)

from plugin_sdk.core import ToolCall  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_dispatcher():
    """Each test starts with a fresh, unbuilt dispatcher slot."""
    reset_for_tests()
    yield
    reset_for_tests()


# ─── shape: bootstrap is idempotent ────────────────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_populates_dispatcher_slot():
    assert get_default_dispatcher_app() is None
    await ensure_dispatcher_app_ready()
    app = get_default_dispatcher_app()
    assert app is not None


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent():
    """Two sequential calls produce the same app instance (no rebuild)."""
    await ensure_dispatcher_app_ready()
    first = get_default_dispatcher_app()
    await ensure_dispatcher_app_ready()
    second = get_default_dispatcher_app()
    assert first is second


@pytest.mark.asyncio
async def test_concurrent_first_calls_single_flight(monkeypatch):
    """Concurrent first-callers must not double-init.

    All ``N`` coroutines must observe the same app object AND
    ``_build_dispatcher_app`` must only be called exactly once. The lock
    inside ``ensure_dispatcher_app_ready`` collapses concurrent first
    calls to a single build.
    """
    from extensions.browser_control import _dispatcher_bootstrap as boot

    build_calls = 0
    real_build = boot._build_dispatcher_app

    async def counted_build():
        nonlocal build_calls
        build_calls += 1
        # Tiny await yield so the racing coroutines all reach the lock
        # before this one finishes building, giving the lock a real
        # chance to serialize them.
        await asyncio.sleep(0)
        return await real_build()

    monkeypatch.setattr(boot, "_build_dispatcher_app", counted_build)

    n = 8
    results = await asyncio.gather(
        *[_capture_app_after_bootstrap() for _ in range(n)]
    )
    distinct = {id(a) for a in results if a is not None}
    assert len(distinct) == 1, f"expected one app object, got {len(distinct)}"
    assert build_calls == 1, f"expected single build, got {build_calls}"


async def _capture_app_after_bootstrap():
    await ensure_dispatcher_app_ready()
    return get_default_dispatcher_app()


# ─── integration: Browser(action='status') without manual bootstrap ────


@pytest.mark.asyncio
async def test_browser_status_works_without_manual_bootstrap():
    """Real production-shape call.

    The test deliberately constructs ``Browser()`` with no ``actions=``
    override so it hits the production code path: lazy bootstrap →
    in-process dispatcher → server.handlers.handle_status → JSON
    response. The status payload is read-only state, so this exercises
    the full pipeline without spawning Chrome.
    """
    tool = Browser()
    result = await tool.execute(
        ToolCall(id="t1", name="Browser", arguments={"action": "status"})
    )
    assert result.is_error is False, (
        f"Browser(action=status) errored: {result.content!r}"
    )
    assert isinstance(result.content, str) and result.content
    # Sanity-check the shape — the status payload includes these keys.
    for k in ("enabled", "profile", "default_profile", "running", "status"):
        assert k in result.content, f"missing key {k!r} in {result.content!r}"


@pytest.mark.asyncio
async def test_browser_second_call_reuses_dispatcher():
    """Calls #2..N do not rebuild — the slot stays pinned to call-1's app."""
    tool = Browser()
    await tool.execute(
        ToolCall(id="t1", name="Browser", arguments={"action": "status"})
    )
    app_after_first = get_default_dispatcher_app()
    await tool.execute(
        ToolCall(id="t2", name="Browser", arguments={"action": "status"})
    )
    app_after_second = get_default_dispatcher_app()
    assert app_after_first is app_after_second
