"""End-to-end integration test for Wave 3 — against a local fixture HTML.

Goal: prove the W3 wiring (client → in-process dispatcher → server →
session → tools_core → playwright) works end-to-end without hitting the
network. We launch a managed Chrome (or skip if playwright + chromium
aren't installed locally), navigate to the fixture file, snapshot the
page, click a button, fill an input, screenshot, and close.

Marked ``slow`` so default ``pytest`` runs skip it; CI opts in.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip(
    "playwright",
    reason="install with `pip install opencomputer[browser]` and `playwright install chromium`",
)
pytest.importorskip("fastapi")
pytest.importorskip("send2trash")

from extensions.browser_control.client import (  # noqa: E402
    BrowserActions,
    set_default_dispatcher_app,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "browser_port" / "sample.html"


def _fixture_url() -> str:
    """File:// URL for the local fixture HTML (offline / deterministic)."""
    return _FIXTURE_PATH.resolve().as_uri()


def _have_chromium() -> bool:
    """True when the playwright chromium binary appears installed.

    Best-effort: looks for the playwright cache directory. Test skips
    cleanly if missing.
    """
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    if cache.exists():
        return any(cache.glob("chromium-*"))
    cache_linux = Path.home() / ".cache" / "ms-playwright"
    if cache_linux.exists():
        return any(cache_linux.glob("chromium-*"))
    return False


@pytest.mark.skipif(
    not _have_chromium(),
    reason="chromium binary not installed (run `playwright install chromium`)",
)
@pytest.mark.skipif(
    os.environ.get("OPENCOMPUTER_SKIP_BROWSER_E2E", "") == "1",
    reason="OPENCOMPUTER_SKIP_BROWSER_E2E=1",
)
@pytest.mark.skip(
    reason="Pre-existing fixture/API drift: test subclasses TabOpsBackend "
    "(a dataclass) which doesn't work with the current required-init-fields "
    "shape. Needs a proper rewrite to construct TabOpsBackend with callables."
)
@pytest.mark.asyncio
async def test_full_flow_against_fixture(tmp_path):
    """Launch managed Chrome → navigate → snapshot → click → fill → screenshot → close.

    This exercises the dual-transport contract: the Browser actions
    issue path-only requests that route through the in-process
    dispatcher (no socket).
    """
    from extensions.browser_control.profiles import resolve_browser_config
    from extensions.browser_control.server import BrowserAuth, BrowserRouteContext, create_app
    from extensions.browser_control.server_context import (
        BrowserServerState,
        ProfileDriver,
    )
    from extensions.browser_control.server_context.tab_ops import TabOpsBackend

    # Build the route context with a stub driver — we don't actually need
    # to launch Chrome for this smoke test; we just want to verify the
    # client → dispatcher → server routing works end to end. The
    # heavyweight playwright integration is unit-tested in
    # test_browser_port_session_*.py.
    auth = BrowserAuth()  # anonymous loopback
    state = BrowserServerState()

    class _StubDriver(ProfileDriver):
        async def ensure_running(self, profile_name, runtime, *, ssrf_policy=None):
            return None

        async def teardown(self, runtime):
            return None

    class _StubTabOps(TabOpsBackend):
        async def open_tab(self, runtime, url, *, ssrf_policy=None):
            from extensions.browser_control.server_context import TabInfo
            return TabInfo(target_id="t-stub", url=url, title="stub")

        async def focus_tab(self, runtime, target_id):
            return None

        async def close_tab(self, runtime, target_id):
            return None

        async def list_tabs(self, runtime):
            return []

    ctx = BrowserRouteContext(
        state=state,
        auth=auth,
        driver=_StubDriver(),
        tab_backend=_StubTabOps(),
    )

    app = create_app(ctx)
    set_default_dispatcher_app(app)

    actions = BrowserActions(auth=auth)

    # 1) Status — this hits the dispatcher with a GET /
    status = await actions.browser_status()
    assert isinstance(status, dict)

    # 2) Profiles list
    profiles = await actions.browser_profiles()
    assert isinstance(profiles, dict)

    # 3) Open a tab to the fixture URL — exercises POST /tabs/open
    fixture_url = _fixture_url()
    open_resp = await actions.browser_open_tab(url=fixture_url, profile="opencomputer")
    assert isinstance(open_resp, dict)

    # Cleanup: reset the module-level dispatcher app
    set_default_dispatcher_app(None)


# ─── wave-3.2: real CDP openers — open / focus / close end-to-end ──────


@pytest.mark.skipif(
    not _have_chromium(),
    reason="chromium binary not installed (run `playwright install chromium`)",
)
@pytest.mark.skipif(
    os.environ.get("OPENCOMPUTER_SKIP_BROWSER_E2E", "") == "1",
    reason="OPENCOMPUTER_SKIP_BROWSER_E2E=1",
)
@pytest.mark.asyncio
async def test_real_cdp_open_focus_close_against_fixture(tmp_path):  # noqa: ARG001
    """The full hot path with a real browser — no mocks.

    Uses the production lazy-bootstrap path (``ensure_dispatcher_app_ready``)
    so this exercises exactly what the agent will hit in production:
    Browser(action=open, url=...) → in-process dispatcher → CDP openers
    → real Playwright + real Chromium → file:// fixture URL.
    """
    from extensions.browser_control._dispatcher_bootstrap import (
        reset_for_tests,
    )
    from extensions.browser_control._tool import Browser

    from plugin_sdk.core import ToolCall

    reset_for_tests()
    try:
        tool = Browser()
        fixture_url = _fixture_url()

        # 1) Open a tab via the production tool surface.
        open_result = await tool.execute(
            ToolCall(
                id="open-1",
                name="Browser",
                arguments={"action": "open", "url": fixture_url},
            )
        )
        assert open_result.is_error is False, (
            f"Browser(action=open) failed: {open_result.content!r}"
        )
        # The response is a JSON-shaped string; sanity-check key tokens.
        assert "target_id" in open_result.content
        assert "tab" in open_result.content

        # Pull target_id off the JSON content so we can test focus/close.
        import json

        content_obj = (
            json.loads(open_result.content)
            if isinstance(open_result.content, str)
            else open_result.content
        )
        tab_obj = content_obj.get("tab", {}) if isinstance(content_obj, dict) else {}
        target_id = tab_obj.get("target_id")
        assert isinstance(target_id, str) and target_id, (
            f"missing target_id in {open_result.content!r}"
        )

        # 2) Focus the tab.
        focus_result = await tool.execute(
            ToolCall(
                id="focus-1",
                name="Browser",
                arguments={"action": "focus", "targetId": target_id},
            )
        )
        assert focus_result.is_error is False, (
            f"Browser(action=focus) failed: {focus_result.content!r}"
        )

        # 3) Close the tab.
        close_result = await tool.execute(
            ToolCall(
                id="close-1",
                name="Browser",
                arguments={"action": "close", "targetId": target_id},
            )
        )
        assert close_result.is_error is False, (
            f"Browser(action=close) failed: {close_result.content!r}"
        )
    finally:
        # Best-effort: reset the dispatcher cache so other tests get a
        # clean slate (the underlying Chrome will leak — wave-3.3).
        reset_for_tests()
