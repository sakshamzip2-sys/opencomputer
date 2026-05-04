"""Unit tests for `server_context/lifecycle.py`."""

from __future__ import annotations

import asyncio

import pytest
from extensions.browser_control.profiles import resolve_browser_config
from extensions.browser_control.server_context import (
    BrowserServerState,
    ProfileDriver,
    ProfileStatus,
    ReconcileMarker,
    ensure_profile_running,
    teardown_profile,
)
from extensions.browser_control.server_context.state import (
    get_or_create_profile_state,
)


def _state() -> BrowserServerState:
    return BrowserServerState(resolved=resolve_browser_config({}))


class _AliveProc:
    """Minimal stub: looks alive to is_running_alive (returncode=None)."""

    returncode: int | None = None

    def poll(self) -> int | None:
        return None


class _FakeRunning:
    """Minimal RunningChrome shape for lifecycle tests.

    Provides ``proc`` (alive) and ``cdp_url`` so the W3.3 liveness probe
    (which calls ``is_chrome_reachable(running.cdp_url)``) has a target
    to mock against.
    """

    def __init__(self, name: str = "opencomputer") -> None:
        self.proc = _AliveProc()
        self.name = name
        self.cdp_url = f"http://127.0.0.1:18793/?fake={name}"


@pytest.fixture(autouse=True)
def _stub_is_chrome_reachable(monkeypatch):
    """Default: liveness probe says reachable (alive). Tests that need the
    'unreachable' path can override via monkeypatch."""

    async def _reachable(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(
        "extensions.browser_control.server_context.lifecycle.is_chrome_reachable",
        _reachable,
    )


# ─── ensure_profile_running ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_local_managed_launches_via_driver() -> None:
    launches: list[str] = []
    connects: list[tuple[str, str]] = []

    async def launch(profile) -> str:
        launches.append(profile.name)
        return f"running-{profile.name}"

    async def connect(profile, running) -> str:
        connects.append((profile.name, running))
        return f"session-{profile.name}"

    state = _state()
    driver = ProfileDriver(launch_managed=launch, connect_managed=connect)
    runtime = await ensure_profile_running(state, "opencomputer", driver=driver)
    assert runtime.status == ProfileStatus.RUNNING
    assert runtime.running == "running-opencomputer"
    assert runtime.playwright_session == "session-opencomputer"
    assert launches == ["opencomputer"]
    assert connects == [("opencomputer", "running-opencomputer")]


@pytest.mark.asyncio
async def test_ensure_chrome_mcp_uses_mcp_driver() -> None:
    spawns: list[str] = []

    async def spawn_mcp(profile) -> str:
        spawns.append(profile.name)
        return f"mcp-client-{profile.name}"

    state = _state()
    driver = ProfileDriver(spawn_chrome_mcp=spawn_mcp)
    runtime = await ensure_profile_running(state, "user", driver=driver)
    assert runtime.status == ProfileStatus.RUNNING
    assert runtime.chrome_mcp_client == "mcp-client-user"
    assert runtime.running is None  # we don't own a process
    assert spawns == ["user"]


@pytest.mark.asyncio
async def test_ensure_is_idempotent_when_already_running() -> None:
    launches: list[str] = []

    async def launch(profile) -> _FakeRunning:
        launches.append(profile.name)
        return _FakeRunning(profile.name)

    state = _state()
    driver = ProfileDriver(launch_managed=launch)
    a = await ensure_profile_running(state, "opencomputer", driver=driver)
    b = await ensure_profile_running(state, "opencomputer", driver=driver)
    assert a is b
    assert launches == ["opencomputer"]


@pytest.mark.asyncio
async def test_ensure_dedupes_concurrent_calls() -> None:
    """Two coroutines hitting ensure for the same profile → one launch."""
    n_launches = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def launch(profile) -> _FakeRunning:
        nonlocal n_launches
        n_launches += 1
        started.set()
        await release.wait()
        return _FakeRunning(profile.name)

    state = _state()
    driver = ProfileDriver(launch_managed=launch)
    t1 = asyncio.create_task(ensure_profile_running(state, "opencomputer", driver=driver))
    t2 = asyncio.create_task(ensure_profile_running(state, "opencomputer", driver=driver))
    await started.wait()
    release.set()
    a, b = await asyncio.gather(t1, t2)
    assert a is b
    assert n_launches == 1


@pytest.mark.asyncio
async def test_ensure_relaunches_when_chrome_died_out_of_band(monkeypatch) -> None:
    """Wave 3.3 + Bug F: status==RUNNING but Chrome unreachable → re-bring-up.

    The HTTP probe (``is_chrome_reachable``) detects out-of-band Chrome
    death (kill -9, crash, OS sigkill — anything that takes Chrome off
    the CDP port). Lifecycle resets state to STOPPED and falls through
    to _bring_up. Without this, Browser actions over the dead WS would
    hang until timeout.

    Bug F fix (2026-05-04) ALSO probes after each fresh ``_bring_up``
    succeeds, so the probe fires three times in this scenario:

      1. After the first launch (verify) — must succeed
      2. On second ``ensure_profile_running`` (pre-RUNNING liveness)
         — must fail to trigger relaunch
      3. After the relaunch (verify) — must succeed
    """
    launches: list[str] = []

    async def launch(profile) -> _FakeRunning:
        launches.append(profile.name)
        return _FakeRunning(profile.name)

    probe_calls: list[None] = []

    async def _probe(*args, **kwargs) -> bool:
        probe_calls.append(None)
        # Only the second probe call (the pre-RUNNING liveness check on
        # the second ensure call) reports unreachable.
        return len(probe_calls) != 2

    monkeypatch.setattr(
        "extensions.browser_control.server_context.lifecycle.is_chrome_reachable",
        _probe,
    )

    state = _state()
    driver = ProfileDriver(launch_managed=launch)

    # First call — fresh launch + verify (probe call #1 → reachable=True).
    a = await ensure_profile_running(state, "opencomputer", driver=driver)
    assert a.status == ProfileStatus.RUNNING
    assert launches == ["opencomputer"]
    old_running = a.running

    # Second call — status==RUNNING; pre-RUNNING liveness (probe call #2)
    # says unreachable → relaunch + verify (probe call #3 → reachable=True).
    b = await ensure_profile_running(state, "opencomputer", driver=driver)
    assert b.status == ProfileStatus.RUNNING
    assert launches == ["opencomputer", "opencomputer"]
    assert b.running is not old_running


@pytest.mark.asyncio
async def test_ensure_propagates_failure_and_marks_stopped() -> None:
    async def launch(profile) -> str:
        raise RuntimeError("boom")

    state = _state()
    driver = ProfileDriver(launch_managed=launch)
    with pytest.raises(RuntimeError, match="boom"):
        await ensure_profile_running(state, "opencomputer", driver=driver)
    runtime = state.profiles["opencomputer"]
    assert runtime.status == ProfileStatus.STOPPED
    assert runtime.last_error == "boom"


@pytest.mark.asyncio
async def test_ensure_missing_profile_raises_lookuperror() -> None:
    state = _state()
    driver = ProfileDriver()
    with pytest.raises(LookupError):
        await ensure_profile_running(state, "no-such-profile", driver=driver)


@pytest.mark.asyncio
async def test_ensure_managed_without_driver_callable_raises() -> None:
    state = _state()
    driver = ProfileDriver()  # no launch_managed
    with pytest.raises(RuntimeError, match="launch_managed"):
        await ensure_profile_running(state, "opencomputer", driver=driver)


# ─── reconcile path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_marker_triggers_teardown_then_relaunch() -> None:
    teardowns: list[str] = []
    launches: list[str] = []

    async def launch(profile) -> str:
        launches.append(profile.name)
        return f"running-{len(launches)}"

    async def stop(running) -> None:
        teardowns.append(str(running))

    state = _state()
    driver = ProfileDriver(launch_managed=launch, stop_managed=stop)
    runtime = await ensure_profile_running(state, "opencomputer", driver=driver)
    assert runtime.running == "running-1"

    # Set reconcile marker — simulate config hot-reload.
    runtime.reconcile = ReconcileMarker(
        previous_profile=runtime.profile, reason="test reconcile"
    )
    again = await ensure_profile_running(state, "opencomputer", driver=driver)
    assert again is runtime
    assert runtime.running == "running-2"
    assert runtime.reconcile is None
    assert teardowns == ["running-1"]


# ─── teardown_profile ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_teardown_managed_calls_stop() -> None:
    stops: list[str] = []

    async def stop(running) -> None:
        stops.append(str(running))

    state = _state()
    profile = state.resolved.profiles["opencomputer"]
    from extensions.browser_control.profiles import resolve_profile

    resolved_profile = resolve_profile(state.resolved, "opencomputer")
    assert resolved_profile is not None
    runtime = get_or_create_profile_state(state, resolved_profile)
    runtime.running = "running-abc"
    runtime.status = ProfileStatus.RUNNING
    driver = ProfileDriver(stop_managed=stop)
    await teardown_profile(runtime, driver=driver)
    assert stops == ["running-abc"]
    assert runtime.status == ProfileStatus.STOPPED
    assert runtime.running is None


@pytest.mark.asyncio
async def test_teardown_chrome_mcp_calls_close() -> None:
    closes: list[str] = []

    async def close(client) -> None:
        closes.append(str(client))

    state = _state()
    from extensions.browser_control.profiles import resolve_profile

    resolved_profile = resolve_profile(state.resolved, "user")
    assert resolved_profile is not None
    runtime = get_or_create_profile_state(state, resolved_profile)
    runtime.chrome_mcp_client = "client-x"
    runtime.status = ProfileStatus.RUNNING
    driver = ProfileDriver(close_chrome_mcp=close)
    await teardown_profile(runtime, driver=driver)
    assert closes == ["client-x"]
    assert runtime.chrome_mcp_client is None


@pytest.mark.asyncio
async def test_teardown_swallows_driver_errors() -> None:
    async def stop(_running) -> None:
        raise RuntimeError("teardown blew up")

    state = _state()
    from extensions.browser_control.profiles import resolve_profile

    resolved_profile = resolve_profile(state.resolved, "opencomputer")
    assert resolved_profile is not None
    runtime = get_or_create_profile_state(state, resolved_profile)
    runtime.running = "running-ouch"
    runtime.status = ProfileStatus.RUNNING
    driver = ProfileDriver(stop_managed=stop)
    # Must not raise.
    await teardown_profile(runtime, driver=driver)
    assert runtime.running is None
    assert runtime.status == ProfileStatus.STOPPED
