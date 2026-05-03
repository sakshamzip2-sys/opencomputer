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
from typing import Any

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


# ─── wave-3.2: CDP tab openers ─────────────────────────────────────────


from extensions.browser_control import _dispatcher_bootstrap as _boot  # noqa: E402
from extensions.browser_control.profiles import (  # noqa: E402
    resolve_browser_config,
    resolve_profile,
)
from extensions.browser_control.server_context import (  # noqa: E402
    ProfileRuntimeState,
    TabInfo,
)


class _FakeProc:
    """Stand-in for ``asyncio.subprocess.Process`` — only ``returncode`` is read."""

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode


class _FakeRunningChrome:
    """Stand-in for ``chrome.launch.RunningChrome``."""

    def __init__(self, *, pid: int = 12345, cdp_url: str = "http://127.0.0.1:18800") -> None:
        self.pid = pid
        self.cdp_url = cdp_url
        self.proc = _FakeProc(returncode=None)


class _FakeBrowserContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self.new_page_calls = 0

    async def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        page = _FakePage(context=self)
        self.pages.append(page)
        return page


class _FakeBrowser:
    def __init__(self, *, contexts: list[_FakeBrowserContext] | None = None) -> None:
        self.contexts = contexts if contexts is not None else [_FakeBrowserContext()]
        self.new_context_calls = 0

    async def new_context(self) -> _FakeBrowserContext:
        self.new_context_calls += 1
        ctx = _FakeBrowserContext()
        self.contexts.append(ctx)
        return ctx

    def on(self, *_args, **_kw) -> None:
        return None


class _FakeCdpSession:
    def __init__(self, target_id: str) -> None:
        self._tid = target_id

    async def send(self, method: str) -> dict:
        if method != "Target.getTargetInfo":
            raise RuntimeError(f"unexpected method {method}")
        return {"targetInfo": {"targetId": self._tid}}

    async def detach(self) -> None:
        return None


_TARGET_COUNTER = {"n": 0}


def _next_tid() -> str:
    _TARGET_COUNTER["n"] += 1
    return f"T-{_TARGET_COUNTER['n']:03d}"


class _FakePage:
    def __init__(self, *, context: _FakeBrowserContext, target_id: str | None = None) -> None:
        self.url = "about:blank"
        self._target_id = target_id or _next_tid()
        self.context_obj = context
        self.goto_calls: list[str] = []
        self.bring_to_front_calls = 0
        self.close_calls = 0
        self._closed = False

    @property
    def context(self) -> Any:
        return _FakePageContext(self._target_id)

    async def goto(self, url: str, timeout: int | None = None) -> None:  # noqa: ARG002
        self.goto_calls.append(url)
        self.url = url

    async def title(self) -> str:
        return f"title-of-{self.url}"

    async def bring_to_front(self) -> None:
        self.bring_to_front_calls += 1

    async def close(self) -> None:
        self.close_calls += 1
        self._closed = True
        # remove self from context's pages so list_tabs reflects it
        try:
            self.context_obj.pages.remove(self)
        except ValueError:
            pass


class _FakePageContext:
    """Returned via ``page.context`` — needs to expose ``new_cdp_session``."""

    def __init__(self, target_id: str) -> None:
        self._tid = target_id

    async def new_cdp_session(self, _page: Any) -> _FakeCdpSession:
        return _FakeCdpSession(self._tid)


@pytest.fixture(autouse=True)
def _reset_target_counter():
    _TARGET_COUNTER["n"] = 0
    yield


def _build_runtime(profile_name: str = "openclaw") -> ProfileRuntimeState:
    cfg = resolve_browser_config({"enabled": True}, {})
    profile = resolve_profile(cfg, profile_name)
    assert profile is not None
    return ProfileRuntimeState(profile=profile)


# --- connect_managed cache --------------------------------------------------


@pytest.mark.asyncio
async def test_connect_managed_caches_per_profile(monkeypatch):
    """Second connect_managed call returns the same session, no re-attach."""
    fake_browser = _FakeBrowser()
    connect_calls = 0

    async def _fake_connect_browser(cdp_url: str, **_kw):
        nonlocal connect_calls
        connect_calls += 1
        from extensions.browser_control.session.cdp import ConnectedBrowser

        return ConnectedBrowser(browser=fake_browser, cdp_url=cdp_url)

    # Patch connect_browser inside the bootstrap-imported module.
    from extensions.browser_control.session import cdp as cdp_mod

    monkeypatch.setattr(cdp_mod, "connect_browser", _fake_connect_browser)
    monkeypatch.setattr(
        "extensions.browser_control.session.connect_browser", _fake_connect_browser
    )

    cfg = resolve_browser_config({"enabled": True}, {})
    profile = resolve_profile(cfg, "openclaw")
    assert profile is not None
    running = _FakeRunningChrome()

    sess1 = await _boot._connect_managed_cached(profile, running)
    sess2 = await _boot._connect_managed_cached(profile, running)
    assert sess1 is sess2
    assert connect_calls == 1


@pytest.mark.asyncio
async def test_launch_managed_short_circuits_when_cached(monkeypatch):
    """If a RunningChrome is already cached for the profile, _launch_managed
    returns it instead of relaunching."""
    cfg = resolve_browser_config({"enabled": True}, {})
    profile = resolve_profile(cfg, "openclaw")
    assert profile is not None

    fake_browser = _FakeBrowser()
    running = _FakeRunningChrome()

    # Pre-populate the cache as if a previous launch had happened.
    from extensions.browser_control.session.cdp import ConnectedBrowser
    from extensions.browser_control.session.playwright_session import PlaywrightSession

    _boot._managed_cache[profile.name] = _boot._ManagedProfileEntry(
        running=running,
        connected=ConnectedBrowser(browser=fake_browser, cdp_url=running.cdp_url),
        session=PlaywrightSession(browser=fake_browser, cdp_url=running.cdp_url),
    )

    launch_calls = 0

    async def _fake_launch(_resolved, _profile):
        nonlocal launch_calls
        launch_calls += 1
        return _FakeRunningChrome()

    monkeypatch.setattr(
        "extensions.browser_control.chrome.launch_openclaw_chrome", _fake_launch
    )

    driver = _boot._build_default_profile_driver()
    out = await driver.launch_managed(profile)
    assert out is running, "should return the cached RunningChrome"
    assert launch_calls == 0


# --- open_tab_via_cdp -------------------------------------------------------


@pytest.mark.asyncio
async def test_open_tab_calls_new_page_and_goto():
    runtime = _build_runtime()
    fake_browser = _FakeBrowser()
    from extensions.browser_control.session.playwright_session import PlaywrightSession

    runtime.playwright_session = PlaywrightSession(
        browser=fake_browser, cdp_url="http://127.0.0.1:18800"
    )

    backend = _boot._build_default_tab_ops_backend()
    tab = await backend.open_tab_via_cdp(runtime, "https://example.com/")
    assert isinstance(tab, TabInfo)
    assert tab.url == "https://example.com/"
    assert tab.target_id  # populated via the fake CDP session
    assert tab.title == "title-of-https://example.com/"

    # The browser's first context should have one new page with one goto call.
    ctx = fake_browser.contexts[0]
    assert ctx.new_page_calls == 1
    assert len(ctx.pages) == 1
    assert ctx.pages[0].goto_calls == ["https://example.com/"]


@pytest.mark.asyncio
async def test_open_tab_creates_context_when_none_exist():
    runtime = _build_runtime()
    fake_browser = _FakeBrowser(contexts=[])
    from extensions.browser_control.session.playwright_session import PlaywrightSession

    runtime.playwright_session = PlaywrightSession(
        browser=fake_browser, cdp_url="http://127.0.0.1:18800"
    )

    backend = _boot._build_default_tab_ops_backend()
    tab = await backend.open_tab_via_cdp(runtime, "https://x.example/")
    assert tab.url == "https://x.example/"
    assert fake_browser.new_context_calls == 1
    assert len(fake_browser.contexts) == 1
    assert fake_browser.contexts[0].new_page_calls == 1


@pytest.mark.asyncio
async def test_open_tab_raises_with_no_session():
    runtime = _build_runtime()
    backend = _boot._build_default_tab_ops_backend()
    with pytest.raises(RuntimeError, match="no PlaywrightSession"):
        await backend.open_tab_via_cdp(runtime, "https://x/")


# --- focus_tab_via_cdp ------------------------------------------------------


@pytest.mark.asyncio
async def test_focus_tab_calls_bring_to_front():
    runtime = _build_runtime()
    fake_browser = _FakeBrowser()
    from extensions.browser_control.session.playwright_session import PlaywrightSession

    runtime.playwright_session = PlaywrightSession(
        browser=fake_browser, cdp_url="http://127.0.0.1:18800"
    )
    backend = _boot._build_default_tab_ops_backend()

    # Open one tab so we have a target_id to focus.
    tab = await backend.open_tab_via_cdp(runtime, "https://focusme.example/")
    page = fake_browser.contexts[0].pages[0]
    assert page.bring_to_front_calls == 0

    await backend.focus_tab_via_cdp(runtime, tab.target_id)
    assert page.bring_to_front_calls == 1


# --- close_tab_via_cdp ------------------------------------------------------


@pytest.mark.asyncio
async def test_close_tab_calls_page_close():
    runtime = _build_runtime()
    fake_browser = _FakeBrowser()
    from extensions.browser_control.session.playwright_session import PlaywrightSession

    runtime.playwright_session = PlaywrightSession(
        browser=fake_browser, cdp_url="http://127.0.0.1:18800"
    )
    backend = _boot._build_default_tab_ops_backend()

    tab = await backend.open_tab_via_cdp(runtime, "https://closeme.example/")
    page = fake_browser.contexts[0].pages[0]
    assert page.close_calls == 0

    await backend.close_tab_via_cdp(runtime, tab.target_id)
    assert page.close_calls == 1


@pytest.mark.asyncio
async def test_close_tab_idempotent_when_already_closed():
    """Closing a target that is no longer findable should not raise."""
    runtime = _build_runtime()
    fake_browser = _FakeBrowser()
    from extensions.browser_control.session.playwright_session import PlaywrightSession

    runtime.playwright_session = PlaywrightSession(
        browser=fake_browser, cdp_url="http://127.0.0.1:18800"
    )
    backend = _boot._build_default_tab_ops_backend()

    # Never opened — direct close on an unknown target should be a no-op.
    await backend.close_tab_via_cdp(runtime, "T-DOESNOTEXIST")


# --- list_tabs reflects open tabs ------------------------------------------


@pytest.mark.asyncio
async def test_list_tabs_returns_open_tabs():
    runtime = _build_runtime()
    fake_browser = _FakeBrowser()
    from extensions.browser_control.session.playwright_session import PlaywrightSession

    runtime.playwright_session = PlaywrightSession(
        browser=fake_browser, cdp_url="http://127.0.0.1:18800"
    )
    backend = _boot._build_default_tab_ops_backend()

    await backend.open_tab_via_cdp(runtime, "https://a.example/")
    await backend.open_tab_via_cdp(runtime, "https://b.example/")
    tabs = await backend.list_tabs(runtime)
    urls = sorted(t.url for t in tabs)
    assert urls == ["https://a.example/", "https://b.example/"]


# --- profile driver wiring --------------------------------------------------


def test_profile_driver_has_connect_managed_wired():
    """Wave-3.2 — the driver's connect_managed slot must no longer be None."""
    driver = _boot._build_default_profile_driver()
    assert driver.connect_managed is not None
    assert driver.launch_managed is not None
    assert driver.stop_managed is not None
    # remote-CDP stays unwired for now
    assert driver.connect_remote is None
    assert driver.disconnect_remote is None


def test_tab_ops_backend_has_cdp_callables_wired():
    """Wave-3.2 — local-managed CDP callables must be present."""
    backend = _boot._build_default_tab_ops_backend()
    assert backend.list_tabs is not None
    assert backend.open_tab_via_cdp is not None
    assert backend.focus_tab_via_cdp is not None
    assert backend.close_tab_via_cdp is not None
    # chrome-mcp / persistent-playwright variants stay unwired
    assert backend.open_tab_via_mcp is None
    assert backend.open_tab_via_playwright is None
