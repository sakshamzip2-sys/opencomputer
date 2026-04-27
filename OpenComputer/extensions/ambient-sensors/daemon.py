"""Foreground sensor daemon — polls ``detect_foreground()`` on a tick interval,
dedups, filters sensitive apps, and publishes ``ForegroundAppEvent`` to the F2
bus.

Designed to run inside the gateway daemon (alongside cron / scheduler) OR as a
standalone process via ``oc ambient daemon``.

Privacy contract enforced here (do NOT relax without a spec change):

1.  Window titles are SHA-256 hashed BEFORE publish — raw titles never reach
    the bus.
2.  Sensitive apps (per the regex filter in :mod:`.sensitive_apps`) are
    redacted at the source: ``app_name="<filtered>"``, ``window_title_hash=""``,
    ``is_sensitive=True``.
3.  Dedup: a publish is skipped if its
    ``(app_name, window_title_hash, bundle_id)`` tuple matches the previous
    publish.
4.  Min-interval: at most one publish per ``_MIN_PUBLISH_INTERVAL_S`` (default
    2 s) even when the foreground changes faster.
5.  Pause-aware: while ``is_currently_paused(state)`` is True, the foreground
    detector is not even called; a single ``AmbientSensorPauseEvent`` fires on
    each transition into / out of paused.
6.  Disabled-aware: while ``state.enabled`` is False, the daemon is fully
    silent — no detect, no publish, no heartbeat.
7.  Heartbeat: when enabled (paused or not), the daemon writes
    ``<profile_home>/ambient/heartbeat`` with ``str(time.time())`` each tick.
    OSError on write is logged at DEBUG and ignored.
8.  None-tolerant: if ``detect()`` returns ``None`` (Wayland, missing tools,
    transient failure), the tick is a silent no-op.
9.  Exception-safe: per-tick exceptions are caught + logged in :meth:`run` so
    the loop survives transient detector / bus failures.

The constructor accepts injectable ``detect``, ``state_loader``, and
``overrides_loader`` callables so unit tests can stub every IO seam without
touching the real filesystem or osascript / xdotool subprocesses.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from plugin_sdk.ingestion import AmbientSensorPauseEvent, ForegroundAppEvent

from .foreground import ForegroundSnapshot, detect_foreground
from .pause_state import AmbientState, is_currently_paused, load_state
from .sensitive_apps import is_sensitive, load_user_overrides

_log = logging.getLogger("opencomputer.ambient.daemon")

_MIN_PUBLISH_INTERVAL_S = 2.0
_HEARTBEAT_FILENAME = "heartbeat"
_FILTERED_APP_NAME = "<filtered>"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


# Default state used when no profile_home is configured (tests + ad-hoc runs).
# Contrast with load_state's disk default (``enabled=False``); we trust the
# caller's choice not to point the daemon at a profile.
_DEFAULT_STATE = AmbientState(enabled=True, sensors=("foreground",))


class ForegroundSensorDaemon:
    """Polls foreground detector, dedups, filters, publishes to bus."""

    def __init__(
        self,
        *,
        bus: Any,  # opencomputer.ingestion.bus.TypedEventBus (duck-typed)
        profile_home_factory: Callable[[], Path | None],
        detect: Callable[[], ForegroundSnapshot | None] = detect_foreground,
        state_loader: Callable[[Path], AmbientState] = load_state,
        overrides_loader: Callable[[Path], list[str]] = load_user_overrides,
        tick_seconds: float = 10.0,
    ) -> None:
        self._bus = bus
        self._profile_home_factory = profile_home_factory
        self._detect = detect
        self._state_loader = state_loader
        self._overrides_loader = overrides_loader
        self._tick_seconds = tick_seconds

        self._last_publish: tuple[str, str, str] | None = None
        self._last_publish_time: float = 0.0
        # ``None`` → no transition seen yet; True/False → last observed state.
        self._last_pause_state: bool | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ tick

    async def _tick(self) -> None:
        """One poll cycle. Raises only on programmer errors; IO/runtime errors
        from the detector / bus are expected to bubble up to :meth:`run`'s
        wrapper, which logs + continues.
        """
        profile_home = self._profile_home_factory()
        state = self._load_state(profile_home)

        if not state.enabled:
            # Reset transition memory so a future enable+pause emits a clean
            # initial pause event rather than being dedup'd against stale state.
            self._last_pause_state = None
            return

        # Heartbeat — written every tick we're enabled, paused or not.
        self._write_heartbeat(profile_home)

        # Pause-state transitions.
        currently_paused = is_currently_paused(state)
        if currently_paused:
            if self._last_pause_state is not True:
                await self._bus.apublish(
                    AmbientSensorPauseEvent(
                        sensor_name="foreground",
                        paused=True,
                        reason="user-paused",
                        source="ambient-sensors",
                    )
                )
                self._last_pause_state = True
            # While paused: skip detect entirely.
            return
        elif self._last_pause_state is True:
            await self._bus.apublish(
                AmbientSensorPauseEvent(
                    sensor_name="foreground",
                    paused=False,
                    reason="resumed",
                    source="ambient-sensors",
                )
            )
            self._last_pause_state = False
        elif self._last_pause_state is None:
            # First tick after enable, never paused → mark observed.
            self._last_pause_state = False

        # Detect foreground.
        snap = self._detect()
        if snap is None:
            _log.debug("detect() returned None; skipping tick")
            return

        # Sensitive filter — applied BEFORE we build the publish payload.
        extras = self._load_overrides(profile_home)
        sensitive = is_sensitive(snap, extra_patterns=extras)

        if sensitive:
            app_name = _FILTERED_APP_NAME
            title_hash = ""
            bundle_id = ""
        else:
            app_name = snap.app_name
            title_hash = _sha256(snap.window_title) if snap.window_title else ""
            bundle_id = snap.bundle_id

        key = (app_name, title_hash, bundle_id)

        # Dedup — same content as last publish → no-op.
        if key == self._last_publish:
            return

        # Min-interval guard.
        now = time.time()
        if now - self._last_publish_time < _MIN_PUBLISH_INTERVAL_S:
            return

        event = ForegroundAppEvent(
            app_name=app_name,
            window_title_hash=title_hash,
            bundle_id=bundle_id,
            is_sensitive=sensitive,
            platform=snap.platform or sys.platform,
            source="ambient-sensors",
        )
        await self._bus.apublish(event)
        self._last_publish = key
        self._last_publish_time = now

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        """Run the poll loop forever (or until cancelled)."""
        _log.info(
            "ambient foreground daemon starting (tick=%ss)", self._tick_seconds
        )
        try:
            while True:
                try:
                    await self._tick()
                except Exception:  # noqa: BLE001 — must survive any per-tick error
                    _log.exception("ambient daemon tick failed")
                await asyncio.sleep(self._tick_seconds)
        except asyncio.CancelledError:
            _log.info("ambient foreground daemon stopped")
            raise

    async def run_once_for_test(self) -> None:
        """Single-tick variant of :meth:`run` for tests of the wrapper.

        Mirrors :meth:`run`'s exception-swallowing behaviour without the loop
        + sleep. Used by the unit test that asserts a raising ``detect()``
        does not propagate.
        """
        try:
            await self._tick()
        except Exception:  # noqa: BLE001
            _log.exception("ambient daemon tick failed")

    def start(self) -> asyncio.Task[None]:
        """Start the daemon as an asyncio task. Returns the task handle."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(
            self.run(), name="ambient-foreground-daemon"
        )
        return self._task

    async def stop(self) -> None:
        """Cancel the running task (if any) and await its teardown."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    # ----------------------------------------------------------- internals

    def _load_state(self, profile_home: Path | None) -> AmbientState:
        # Honour the injected ``state_loader`` whether or not ``profile_home``
        # is set — tests inject a non-default loader and expect it to win even
        # when there's no real profile dir on disk. Production callers always
        # set ``profile_home`` so the default ``load_state`` reads
        # ``<home>/ambient/state.json``; the ``None`` path falls back to
        # ``_DEFAULT_STATE`` only when the loader is the on-disk default
        # (which can't read from a missing path).
        if profile_home is not None:
            state_path = profile_home / "ambient" / "state.json"
            return self._state_loader(state_path)
        if self._state_loader is not load_state:
            # Test seam: injected loader doesn't need a real path.
            return self._state_loader(Path())
        return _DEFAULT_STATE

    def _load_overrides(self, profile_home: Path | None) -> list[str]:
        if profile_home is not None:
            override_path = profile_home / "ambient" / "sensitive_apps.txt"
            return self._overrides_loader(override_path)
        if self._overrides_loader is not load_user_overrides:
            return self._overrides_loader(Path())
        return []

    def _write_heartbeat(self, profile_home: Path | None) -> None:
        if profile_home is None:
            return
        hb = profile_home / "ambient" / _HEARTBEAT_FILENAME
        try:
            hb.parent.mkdir(parents=True, exist_ok=True)
            hb.write_text(str(time.time()))
        except OSError:
            _log.debug("heartbeat write failed", exc_info=True)
