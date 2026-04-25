"""
:class:`BehavioralInferenceEngine` — F2 default-bus subscriber + extractor runner.

Subscribes to :data:`opencomputer.ingestion.bus.default_bus` (wildcard,
all event types), buffers events, and flushes through the configured
:class:`plugin_sdk.inference.MotifExtractor` instances when either:

* the buffer reaches ``batch_size`` events, or
* ``batch_seconds`` have elapsed since the last flush.

Each extractor invocation is exception-isolated — a broken extractor
logs a WARNING but the others still run. The bus subscriber callback
itself is also exception-isolated (defense in depth on top of the
bus's per-subscriber try/except).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence

from opencomputer.inference.extractors import (
    ImplicitGoalExtractor,
    TemporalMotifExtractor,
    TransitionChainExtractor,
)
from opencomputer.inference.storage import MotifStore
from plugin_sdk.inference import Motif, MotifExtractor
from plugin_sdk.ingestion import SignalEvent

_log = logging.getLogger("opencomputer.inference.engine")

#: Default flush triggers — match values in the public 3.B contract.
DEFAULT_BATCH_SIZE = 100
DEFAULT_BATCH_SECONDS = 300.0


class BehavioralInferenceEngine:
    """Bus-attached buffer + extractor runner.

    Parameters
    ----------
    store:
        :class:`MotifStore` to persist into. ``None`` constructs a
        store at the standard ``<profile_home>/inference/motifs.sqlite``
        path.
    extractors:
        List of :class:`MotifExtractor` instances. ``None`` (the
        production default) uses the three Phase 3.B extractors:
        :class:`TemporalMotifExtractor`,
        :class:`TransitionChainExtractor`,
        :class:`ImplicitGoalExtractor`.
    batch_size:
        Buffer-length threshold for an automatic flush. Default 100.
    batch_seconds:
        Time threshold for an automatic flush. Default 300.0 seconds
        (5 minutes).

    Concurrency
    -----------

    The buffer is guarded by a :class:`threading.Lock`. Bus publishes
    are sync (in-process fanout), so the lock is rarely contended.
    ``flush_now`` snapshots the buffer under the lock and clears it
    before running extractors — extractors run unlocked.
    """

    def __init__(
        self,
        *,
        store: MotifStore | None = None,
        extractors: Sequence[MotifExtractor] | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_seconds: float = DEFAULT_BATCH_SECONDS,
    ) -> None:
        self._store = store if store is not None else MotifStore()
        self._extractors: tuple[MotifExtractor, ...] = (
            tuple(extractors)
            if extractors is not None
            else (
                TemporalMotifExtractor(),
                TransitionChainExtractor(),
                ImplicitGoalExtractor(),
            )
        )
        self.batch_size = int(batch_size)
        self.batch_seconds = float(batch_seconds)

        self._lock = threading.Lock()
        self._buffer: list[SignalEvent] = []
        self._last_flush_at: float = time.monotonic()
        self._subscription = None  # plugin_sdk.ingestion.bus.Subscription | None

    # ─── attach / detach ─────────────────────────────────────────────

    def attach_to_bus(self) -> None:
        """Subscribe to :data:`default_bus` for ALL events.

        Idempotent — calling twice without :meth:`detach` does nothing
        (the second subscription would double-buffer events).
        """
        if self._subscription is not None:
            return
        from opencomputer.ingestion.bus import default_bus

        self._subscription = default_bus.subscribe(None, self._on_event)

    def detach(self) -> None:
        """Unsubscribe and stop buffering. Idempotent."""
        sub = self._subscription
        if sub is None:
            return
        self._subscription = None
        try:
            sub.unsubscribe()
        except Exception:  # noqa: BLE001 — detach must never raise
            _log.debug(
                "inference engine: subscription.unsubscribe() raised "
                "(non-fatal)",
                exc_info=True,
            )

    # ─── bus callback ────────────────────────────────────────────────

    def _on_event(self, event: SignalEvent) -> None:
        """Bus subscriber. Exception-isolated.

        The bus already wraps subscribers in try/except, but this
        defense-in-depth try/except keeps every code path inside the
        engine clean — a logic error in the buffer-management
        codepath shouldn't propagate out via the bus's WARNING log.
        """
        try:
            should_flush = False
            with self._lock:
                self._buffer.append(event)
                size_threshold = len(self._buffer) >= self.batch_size
                time_threshold = (
                    time.monotonic() - self._last_flush_at
                    >= self.batch_seconds
                )
                if size_threshold or time_threshold:
                    should_flush = True
            if should_flush:
                self.flush_now()
        except Exception:  # noqa: BLE001 — engine reliability
            _log.warning(
                "inference engine: _on_event raised; continuing",
                exc_info=True,
            )

    # ─── flush ───────────────────────────────────────────────────────

    def flush_now(self) -> int:
        """Drain the buffer, run extractors, persist, return motif count.

        Each extractor is invoked in its own try/except — a broken
        extractor logs a WARNING and yields zero motifs but does not
        block the others.
        """
        with self._lock:
            if not self._buffer:
                self._last_flush_at = time.monotonic()
                return 0
            batch = list(self._buffer)
            self._buffer.clear()
            self._last_flush_at = time.monotonic()

        all_motifs: list[Motif] = []
        for extractor in self._extractors:
            try:
                motifs = extractor.extract(batch)
            except Exception:  # noqa: BLE001 — per-extractor isolation
                _log.warning(
                    "inference engine: extractor %r raised; skipping",
                    getattr(extractor, "name", type(extractor).__name__),
                    exc_info=True,
                )
                continue
            all_motifs.extend(motifs)

        if all_motifs:
            try:
                self._store.insert_many(all_motifs)
            except Exception:  # noqa: BLE001 — persistence isolation
                _log.warning(
                    "inference engine: store.insert_many raised; "
                    "%d motifs lost from this batch",
                    len(all_motifs),
                    exc_info=True,
                )
                return 0
        return len(all_motifs)

    # ─── introspection ───────────────────────────────────────────────

    @property
    def buffer_size(self) -> int:
        """Current buffer depth (debug visibility)."""
        with self._lock:
            return len(self._buffer)

    @property
    def attached(self) -> bool:
        """Whether :meth:`attach_to_bus` has been called."""
        return self._subscription is not None

    @property
    def store(self) -> MotifStore:
        """The configured :class:`MotifStore` (read-only access)."""
        return self._store


__all__ = [
    "BehavioralInferenceEngine",
    "DEFAULT_BATCH_SECONDS",
    "DEFAULT_BATCH_SIZE",
]
