"""tests/test_ambient_daemon_dedup.py — sensor daemon contract tests."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from extensions.ambient_sensors.daemon import ForegroundSensorDaemon
from extensions.ambient_sensors.foreground import ForegroundSnapshot
from extensions.ambient_sensors.pause_state import AmbientState

from plugin_sdk.ingestion import AmbientSensorPauseEvent, ForegroundAppEvent


def _snap(app: str, title: str = "x", bundle: str = "") -> ForegroundSnapshot:
    return ForegroundSnapshot(
        app_name=app, window_title=title, bundle_id=bundle, platform="linux"
    )


def _make_daemon(
    *,
    detect_returns,
    state=None,
    extra_patterns=None,
    profile_home=None,
):
    """Construct a daemon with all dependencies stubbed."""
    if state is None:
        state = AmbientState(enabled=True, sensors=("foreground",))

    bus = MagicMock()
    bus.apublish = AsyncMock()

    detect_iter = (
        iter(detect_returns) if isinstance(detect_returns, (list, tuple)) else None
    )

    def detect_fn():
        if detect_iter is not None:
            return next(detect_iter)
        return detect_returns

    def state_loader(_path):
        return state

    def overrides_loader(_path):
        return extra_patterns or []

    daemon = ForegroundSensorDaemon(
        bus=bus,
        profile_home_factory=lambda: profile_home,
        detect=detect_fn,
        state_loader=state_loader,
        overrides_loader=overrides_loader,
    )
    return daemon, bus


@pytest.mark.asyncio
async def test_publishes_first_snapshot():
    daemon, bus = _make_daemon(detect_returns=_snap("Code"))
    await daemon._tick()
    assert bus.apublish.await_count == 1
    event = bus.apublish.await_args[0][0]
    assert isinstance(event, ForegroundAppEvent)
    assert event.app_name == "Code"
    assert event.is_sensitive is False


@pytest.mark.asyncio
async def test_dedup_skips_identical_snapshots():
    daemon, bus = _make_daemon(detect_returns=_snap("Code"))
    await daemon._tick()
    # Bypass min-interval guard for the test
    daemon._last_publish_time = 0.0
    await daemon._tick()
    daemon._last_publish_time = 0.0
    await daemon._tick()
    assert bus.apublish.await_count == 1


@pytest.mark.asyncio
async def test_dedup_publishes_when_app_changes():
    snaps = [_snap("Code"), _snap("Safari"), _snap("Safari"), _snap("TradingView")]
    daemon, bus = _make_daemon(detect_returns=snaps)
    for _ in range(4):
        daemon._last_publish_time = 0.0  # bypass min-interval each time
        await daemon._tick()
    assert bus.apublish.await_count == 3
    apps = [call.args[0].app_name for call in bus.apublish.await_args_list]
    assert apps == ["Code", "Safari", "TradingView"]


@pytest.mark.asyncio
async def test_min_interval_guards_rapid_changes():
    """Two rapid snapshots with different content < 2s apart: only one publishes."""
    snaps = [_snap("Code"), _snap("Safari")]
    daemon, bus = _make_daemon(detect_returns=snaps)
    await daemon._tick()  # publishes Code
    # Don't reset _last_publish_time — second tick fires immediately
    await daemon._tick()  # should be guarded
    assert bus.apublish.await_count == 1


@pytest.mark.asyncio
async def test_sensitive_app_filtered_before_publish():
    daemon, bus = _make_daemon(
        detect_returns=_snap("1Password 7", title="Personal Vault")
    )
    await daemon._tick()
    assert bus.apublish.await_count == 1
    event = bus.apublish.await_args[0][0]
    assert event.app_name == "<filtered>"
    assert event.window_title_hash == ""
    assert event.is_sensitive is True


@pytest.mark.asyncio
async def test_window_title_is_hashed_not_plaintext():
    daemon, bus = _make_daemon(
        detect_returns=_snap("Code", title="my-secret-project.py - VS Code")
    )
    await daemon._tick()
    event = bus.apublish.await_args[0][0]
    assert "my-secret-project" not in event.window_title_hash
    assert len(event.window_title_hash) == 64  # SHA-256 hex


@pytest.mark.asyncio
async def test_skips_when_detect_returns_none():
    daemon, bus = _make_daemon(detect_returns=None)
    await daemon._tick()
    assert bus.apublish.await_count == 0


@pytest.mark.asyncio
async def test_skips_when_state_disabled():
    daemon, bus = _make_daemon(
        detect_returns=_snap("Code"),
        state=AmbientState(enabled=False),
    )
    await daemon._tick()
    assert bus.apublish.await_count == 0


@pytest.mark.asyncio
async def test_emits_pause_event_on_transition_into_paused():
    paused_state = AmbientState(
        enabled=True, paused_until=time.time() + 60, sensors=("foreground",)
    )
    daemon, bus = _make_daemon(detect_returns=_snap("Code"), state=paused_state)
    await daemon._tick()
    # Should publish ONE pause event (transition), not the foreground event
    assert bus.apublish.await_count == 1
    event = bus.apublish.await_args[0][0]
    assert isinstance(event, AmbientSensorPauseEvent)
    assert event.paused is True


@pytest.mark.asyncio
async def test_does_not_emit_pause_event_repeatedly():
    """On each subsequent tick while still paused, no new pause event."""
    paused_state = AmbientState(
        enabled=True, paused_until=time.time() + 60, sensors=("foreground",)
    )
    daemon, bus = _make_daemon(detect_returns=_snap("Code"), state=paused_state)
    await daemon._tick()  # transition → 1 pause event
    await daemon._tick()  # still paused → no new event
    await daemon._tick()  # still paused → no new event
    assert bus.apublish.await_count == 1


@pytest.mark.asyncio
async def test_emits_resume_event_when_pause_expires():
    """Transition out of paused → exactly one resume event."""
    state_holder = {
        "state": AmbientState(
            enabled=True, paused_until=time.time() + 60, sensors=("foreground",)
        )
    }

    bus = MagicMock()
    bus.apublish = AsyncMock()
    daemon = ForegroundSensorDaemon(
        bus=bus,
        profile_home_factory=lambda: None,
        detect=lambda: _snap("Code"),
        state_loader=lambda _: state_holder["state"],
        overrides_loader=lambda _: [],
    )
    await daemon._tick()  # paused → 1 pause event

    # Lift the pause
    state_holder["state"] = AmbientState(
        enabled=True, paused_until=None, sensors=("foreground",)
    )
    await daemon._tick()  # resume event + maybe first foreground

    # Either of: resume + first foreground (2 events) OR resume only (1) — both
    # are valid as long as there's exactly ONE resume event among them.
    resume_events = [
        e
        for call in bus.apublish.await_args_list
        for e in [call.args[0]]
        if isinstance(e, AmbientSensorPauseEvent) and e.paused is False
    ]
    assert len(resume_events) == 1


@pytest.mark.asyncio
async def test_heartbeat_written_when_enabled(tmp_path):
    daemon, _ = _make_daemon(detect_returns=_snap("Code"), profile_home=tmp_path)
    await daemon._tick()
    hb = tmp_path / "ambient" / "heartbeat"
    assert hb.exists()
    assert float(hb.read_text()) > 0


@pytest.mark.asyncio
async def test_heartbeat_not_written_when_disabled(tmp_path):
    daemon, _ = _make_daemon(
        detect_returns=_snap("Code"),
        state=AmbientState(enabled=False),
        profile_home=tmp_path,
    )
    await daemon._tick()
    hb = tmp_path / "ambient" / "heartbeat"
    assert not hb.exists()


@pytest.mark.asyncio
async def test_tick_swallows_exceptions():
    """If detect() raises, the run loop wrapper should NOT propagate."""
    bus = MagicMock()
    bus.apublish = AsyncMock()

    def boom():
        raise RuntimeError("simulated detector failure")

    daemon = ForegroundSensorDaemon(
        bus=bus,
        profile_home_factory=lambda: None,
        detect=boom,
        state_loader=lambda _: AmbientState(enabled=True, sensors=("foreground",)),
        overrides_loader=lambda _: [],
    )
    # run_once_for_test wraps a single tick in the same try/except as run().
    # Should not raise.
    await daemon.run_once_for_test()
    assert bus.apublish.await_count == 0


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle():
    """start() returns a task; stop() cancels it cleanly."""
    daemon, _ = _make_daemon(detect_returns=_snap("Code"))
    daemon._tick_seconds = 0.05  # fast tick so we don't hang the test
    task = daemon.start()
    assert task is not None
    assert not task.done()
    await daemon.stop()
    assert task.done()
