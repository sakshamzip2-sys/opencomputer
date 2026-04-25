"""
Bus-attached scheduler for decay + drift passes (Phase 3.D, F5 layer).

Attaches a wildcard subscriber to the :class:`opencomputer.ingestion.bus.TypedEventBus`
that checks, on every Nth event, whether enough wall-clock time has
elapsed since the last decay / drift run. Heavy work (full-graph
decay + KL sweep) is dispatched to a daemon :class:`threading.Thread`
so the bus publish path never blocks.

Design stance
-------------

* **Bus must stay cheap.** The subscriber itself does nothing more than
  a counter increment + a ``time.monotonic()`` comparison per event.
* **One runner per job at a time.** If a previous decay / drift thread
  is still alive, a fresh trigger is silently skipped rather than
  piling threads on top of each other.
* **Detach is soft.** :meth:`detach` just unsubscribes — in-flight
  threads finish naturally. A caller that really needs to observe
  quiescence should poll :meth:`is_running`.
* **Failures never propagate.** Thread bodies swallow exceptions and
  log at WARNING so a bad decay pass cannot break subsequent events.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from opencomputer.ingestion.bus import (
    Subscription,
    TypedEventBus,
    get_default_bus,
)
from plugin_sdk.decay import DriftReport

if TYPE_CHECKING:
    from opencomputer.user_model.decay import DecayEngine
    from opencomputer.user_model.drift import DriftDetector
    from plugin_sdk.ingestion import SignalEvent

_log = logging.getLogger("opencomputer.user_model.scheduler")

DEFAULT_EVENT_CHECK_INTERVAL: int = 100
"""Default: re-check elapsed time every 100 events. Keeps the bus path near-free."""


class DecayDriftScheduler:
    """Wildcard bus subscriber that fires decay + drift on a time budget.

    Parameters
    ----------
    decay_engine:
        :class:`DecayEngine` instance used by the decay thread.
    drift_detector:
        :class:`DriftDetector` instance used by the drift thread.
    decay_interval_seconds:
        Minimum seconds between decay passes. Default 86400 (24h).
    drift_interval_seconds:
        Minimum seconds between drift passes. Default 86400 (24h).
    event_check_interval:
        Check elapsed time only on every Nth event. Default 100 —
        amortises the few microseconds of bookkeeping per event.
    bus:
        Override the bus. ``None`` uses
        :func:`opencomputer.ingestion.bus.get_default_bus`.
    """

    def __init__(
        self,
        *,
        decay_engine: DecayEngine,
        drift_detector: DriftDetector,
        decay_interval_seconds: float = 86400.0,
        drift_interval_seconds: float = 86400.0,
        event_check_interval: int = DEFAULT_EVENT_CHECK_INTERVAL,
        bus: TypedEventBus | None = None,
    ) -> None:
        self.decay_engine = decay_engine
        self.drift_detector = drift_detector
        self.decay_interval_seconds = float(decay_interval_seconds)
        self.drift_interval_seconds = float(drift_interval_seconds)
        self.event_check_interval = max(1, int(event_check_interval))
        self._bus = bus if bus is not None else get_default_bus()
        self._subscription: Subscription | None = None
        self._event_count = 0
        # Initialise the "last run" stamps to the scheduler construction
        # time so the very first event doesn't immediately trigger a
        # full decay pass. The first real run is ``interval`` seconds out.
        self._last_decay_at: float = time.monotonic()
        self._last_drift_at: float = time.monotonic()
        self._decay_thread: threading.Thread | None = None
        self._drift_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ─── manual trigger ───────────────────────────────────────────────

    def run_now(self) -> tuple[int, DriftReport]:
        """Run decay + drift synchronously and return ``(decay_count, report)``.

        Intended for CLI / tests — skips the background-thread plumbing
        so the caller can observe outcomes directly.
        """
        decay_count = self.decay_engine.apply_decay()
        report = self.drift_detector.detect()
        now = time.monotonic()
        self._last_decay_at = now
        self._last_drift_at = now
        return decay_count, report

    # ─── bus attach / detach ──────────────────────────────────────────

    def attach_to_bus(self) -> Subscription:
        """Subscribe to every event. Idempotent — re-attach reuses handle."""
        if self._subscription is not None:
            return self._subscription
        self._subscription = self._bus.subscribe(None, self._on_event)
        return self._subscription

    def detach(self) -> None:
        """Unsubscribe. In-flight decay / drift threads finish naturally."""
        if self._subscription is None:
            return
        self._subscription.unsubscribe()
        self._subscription = None

    # ─── thread-status introspection ──────────────────────────────────

    def is_running(self) -> bool:
        """Return True if either the decay or drift thread is alive."""
        return (
            (self._decay_thread is not None and self._decay_thread.is_alive())
            or (self._drift_thread is not None and self._drift_thread.is_alive())
        )

    # ─── event callback ───────────────────────────────────────────────

    def _on_event(self, _event: SignalEvent) -> None:
        """Bus handler — cheap counter + time check, spawns work off-thread."""
        self._event_count += 1
        if self._event_count % self.event_check_interval != 0:
            return
        now = time.monotonic()
        if now - self._last_decay_at >= self.decay_interval_seconds:
            self._maybe_start_decay(now)
        if now - self._last_drift_at >= self.drift_interval_seconds:
            self._maybe_start_drift(now)

    # ─── background threads ───────────────────────────────────────────

    def _maybe_start_decay(self, now: float) -> None:
        """Spawn a decay thread unless one is already running."""
        with self._lock:
            if self._decay_thread is not None and self._decay_thread.is_alive():
                return
            self._last_decay_at = now
            t = threading.Thread(
                target=self._run_decay,
                daemon=True,
                name="oc-decay-engine",
            )
            self._decay_thread = t
        t.start()

    def _maybe_start_drift(self, now: float) -> None:
        """Spawn a drift thread unless one is already running."""
        with self._lock:
            if self._drift_thread is not None and self._drift_thread.is_alive():
                return
            self._last_drift_at = now
            t = threading.Thread(
                target=self._run_drift,
                daemon=True,
                name="oc-drift-detector",
            )
            self._drift_thread = t
        t.start()

    def _run_decay(self) -> None:
        """Thread body — invoke decay, log at WARNING on failure."""
        try:
            updated = self.decay_engine.apply_decay()
            _log.debug("decay pass complete: updated=%d edges", updated)
        except Exception as exc:  # noqa: BLE001 — background passes must not propagate
            _log.warning("decay pass failed (non-fatal): %s", exc, exc_info=True)

    def _run_drift(self) -> None:
        """Thread body — invoke drift detection, log at WARNING on failure."""
        try:
            report = self.drift_detector.detect()
            _log.debug(
                "drift pass complete: report_id=%s total_kl=%.4f significant=%s",
                report.report_id,
                report.total_kl_divergence,
                report.significant,
            )
        except Exception as exc:  # noqa: BLE001 — background passes must not propagate
            _log.warning("drift pass failed (non-fatal): %s", exc, exc_info=True)


__all__ = ["DecayDriftScheduler", "DEFAULT_EVENT_CHECK_INTERVAL"]
