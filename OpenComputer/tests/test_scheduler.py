"""Tests for :class:`opencomputer.user_model.scheduler.DecayDriftScheduler`.

Validates:
* ``run_now`` invokes both engines synchronously.
* ``attach_to_bus`` subscribes a wildcard handler.
* Throttling — multiple events within the interval trigger only one run.
* ``detach`` unsubscribes cleanly.
* A raising decay engine does not break bus delivery for other subscribers.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from opencomputer.inference.storage import MotifStore
from opencomputer.ingestion.bus import TypedEventBus
from opencomputer.user_model.decay import DecayEngine
from opencomputer.user_model.drift import DriftDetector
from opencomputer.user_model.scheduler import DecayDriftScheduler
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.decay import DriftReport
from plugin_sdk.ingestion import ToolCallEvent


def _engines(tmp_path: Path) -> tuple[DecayEngine, DriftDetector]:
    user_db = tmp_path / "graph.sqlite"
    motif_db = tmp_path / "motifs.sqlite"
    decay = DecayEngine(store=UserModelStore(db_path=user_db))
    drift = DriftDetector(motif_store=MotifStore(db_path=motif_db))
    return decay, drift


class _CountingDecay:
    """Test double for :class:`DecayEngine` — counts ``apply_decay`` calls."""

    def __init__(self) -> None:
        self.calls = 0

    def apply_decay(self, **_kwargs: object) -> int:
        self.calls += 1
        return 0


class _CountingDrift:
    """Test double for :class:`DriftDetector` — counts ``detect`` calls."""

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, **_kwargs: object) -> DriftReport:
        self.calls += 1
        return DriftReport()


def test_run_now_invokes_decay_and_drift() -> None:
    """``run_now`` calls both engines once and returns their outputs."""
    decay = _CountingDecay()
    drift = _CountingDrift()
    sched = DecayDriftScheduler(
        decay_engine=decay,  # type: ignore[arg-type]
        drift_detector=drift,  # type: ignore[arg-type]
    )
    n, report = sched.run_now()
    assert decay.calls == 1
    assert drift.calls == 1
    assert n == 0
    assert isinstance(report, DriftReport)


def test_scheduler_attaches_to_bus(tmp_path: Path) -> None:
    """``attach_to_bus`` registers a wildcard subscriber."""
    bus = TypedEventBus()
    decay, drift = _engines(tmp_path)
    sched = DecayDriftScheduler(
        decay_engine=decay,
        drift_detector=drift,
        bus=bus,
    )
    assert len(bus.subscribers()) == 0
    sub = sched.attach_to_bus()
    assert sub is not None
    assert len(bus.subscribers()) == 1
    # Wildcard — matches any event type.
    assert bus.subscribers("tool_call") == [sub]


def test_scheduler_throttles_decay_by_interval() -> None:
    """Multiple events within the interval produce only one decay run."""
    bus = TypedEventBus()
    decay = _CountingDecay()
    drift = _CountingDrift()
    # Decay ought to trigger on EVERY event_check_interval crossing once
    # enough time has passed. For deterministic testing, set the interval
    # to 0 so the very first check triggers — then the "already running"
    # guard prevents re-entry while the first thread is still alive.
    sched = DecayDriftScheduler(
        decay_engine=decay,  # type: ignore[arg-type]
        drift_detector=drift,  # type: ignore[arg-type]
        decay_interval_seconds=3600.0,  # 1h — won't hit again
        drift_interval_seconds=3600.0,
        event_check_interval=1,  # check every event
        bus=bus,
    )
    sched.attach_to_bus()
    # Force the "elapsed interval" on the very first check by stomping
    # the last-run stamp.
    sched._last_decay_at = time.monotonic() - 7200.0
    sched._last_drift_at = time.monotonic() - 7200.0
    # Fire a burst — only the first should spawn the thread; the rest
    # are throttled by the interval (last_decay_at has been advanced).
    for i in range(5):
        bus.publish(ToolCallEvent(tool_name=f"t{i}"))
    # Give any spawned thread a moment to finish.
    deadline = time.monotonic() + 2.0
    while sched.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    # Exactly one decay run, exactly one drift run.
    assert decay.calls == 1
    assert drift.calls == 1


def test_scheduler_detaches_cleanly(tmp_path: Path) -> None:
    """``detach`` removes the subscription and is idempotent."""
    bus = TypedEventBus()
    decay, drift = _engines(tmp_path)
    sched = DecayDriftScheduler(
        decay_engine=decay,
        drift_detector=drift,
        bus=bus,
    )
    sched.attach_to_bus()
    assert len(bus.subscribers()) == 1
    sched.detach()
    assert len(bus.subscribers()) == 0
    # Idempotent.
    sched.detach()
    assert len(bus.subscribers()) == 0


def test_scheduler_handles_decay_exception(caplog) -> None:
    """A broken decay engine logs WARNING but does not abort the bus."""

    class _BoomDecay:
        def apply_decay(self, **_kwargs: object) -> int:
            raise RuntimeError("simulated decay failure")

    class _NoOpDrift:
        def detect(self, **_kwargs: object) -> DriftReport:
            return DriftReport()

    bus = TypedEventBus()
    # Another subscriber that we'll assert remains alive.
    other_received: list[object] = []
    bus.subscribe("tool_call", other_received.append)

    sched = DecayDriftScheduler(
        decay_engine=_BoomDecay(),  # type: ignore[arg-type]
        drift_detector=_NoOpDrift(),  # type: ignore[arg-type]
        decay_interval_seconds=0.0,
        drift_interval_seconds=3600.0,
        event_check_interval=1,
        bus=bus,
    )
    sched.attach_to_bus()
    sched._last_decay_at = time.monotonic() - 7200.0
    with caplog.at_level(logging.WARNING, logger="opencomputer.user_model.scheduler"):
        bus.publish(ToolCallEvent(tool_name="boom"))
        # Wait for the thread to finish.
        deadline = time.monotonic() + 2.0
        while sched.is_running() and time.monotonic() < deadline:
            time.sleep(0.01)
    # The broken decay thread logged the failure at WARNING.
    assert any(
        "decay pass failed" in rec.getMessage() for rec in caplog.records
    )
    # And the other subscriber still received the event.
    assert len(other_received) == 1
