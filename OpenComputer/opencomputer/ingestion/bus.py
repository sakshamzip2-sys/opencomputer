"""
TypedEventBus — in-memory pub/sub over :class:`plugin_sdk.ingestion.SignalEvent`.

This is the foundational broadcast primitive shipped in Phase 3.A
(F2 foundation). Session B's B3 trajectory subscriber attaches here;
Phases 4/5 (F6 OpenCLI scraper, F7 OI bridge) publish here; Phase 3.B
behavioral inference reads from here.

Design stance
-------------

* **Bus reliability is paramount.** One bad subscriber MUST NOT poison
  others. Every handler invocation is wrapped in try/except; exceptions
  are logged at WARNING and the fanout continues.
* **Publish is sync and fast.** The publish path is an in-process
  fanout — no disk I/O, no network. Subscribers that need heavy work
  queue their own background tasks. A subscriber that blocks the
  publish thread is considered a bug.
* **Bounded queue with drop-oldest backpressure.** The bus keeps a
  bounded :class:`collections.deque` of published events for replay /
  debugging / dropped-count accounting. Default maxlen = 10 000. When
  the queue is full, the oldest event is dropped, ``dropped_count``
  increments, and a WARNING is logged (at most once per warning
  throttle window to avoid log flooding).
* **Thread-safety.** Subscribe / unsubscribe hold a lock; publish
  takes a snapshot of the subscriber list before iterating, so
  concurrent ``unsubscribe()`` during a publish is safe.
* **No persistence.** Events are not durable across process restarts.
  Phase 3.D drift detection may add a SQLite-backed persistent bus;
  see the TODO below.

TODO(phase-3.D): add optional SQLite-backed persistence for drift
analysis. This module intentionally ships as in-memory only to keep
the minimal viable contract lean — Session B's B3 subscriber doesn't
need persistence either. Follow the ``opencomputer/agent/state.py``
SQLite+WAL pattern when adding.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from plugin_sdk.ingestion import SignalEvent

_log = logging.getLogger("opencomputer.ingestion.bus")

#: Subscriber callables can be sync (return None) or async (return an
#: Awaitable). ``publish`` skips awaiting; ``apublish`` awaits them.
type Handler = Callable[[SignalEvent], Any]

DEFAULT_QUEUE_MAXLEN: int = 10_000
"""Default bounded queue size for the drop-oldest backpressure policy."""

DEFAULT_DROP_WARN_INTERVAL_SECONDS: float = 5.0
"""Minimum seconds between drop-oldest WARNING log emissions."""


class BackpressurePolicy(str, Enum):
    """Per-subscription backpressure strategy (``apublish`` only).

    Sync ``publish`` does not queue per-subscriber — it fans out
    synchronously in the publish thread; a slow handler is always a
    bug. ``apublish`` awaits each async handler, so subscribers that
    expect bursts may choose to drop-instead-of-await.

    * ``BLOCK`` — await the handler normally (default).
    * ``DROP`` — if the handler hasn't finished processing the prior
      event when a new one arrives, skip the new event silently.
      Minimalist; no log spam.
    * ``LOG_AND_DROP`` — like ``DROP``, but log at WARNING.

    NOTE: The in-memory queue is separate from this per-subscription
    policy; the queue's drop-oldest behaviour applies to ALL
    subscribers together and is primarily a visibility /
    observability mechanism.
    """

    BLOCK = "block"
    DROP = "drop"
    LOG_AND_DROP = "log_and_drop"


@dataclass(frozen=True, slots=True)
class Subscription:
    """Handle returned by :meth:`TypedEventBus.subscribe`.

    The dataclass is frozen for hashability / immutability; mutable
    state (the ``_busy`` flag used by DROP policies) lives on an
    internal mirror held by the bus itself.
    """

    id: str
    #: ``None`` = wildcard (every event). A bare event-type string
    #: (e.g. ``"tool_call"``) matches by equality. A glob pattern
    #: (e.g. ``"web_*"``) matches via :func:`fnmatch.fnmatchcase`
    #: when ``is_pattern`` is True.
    event_type: str | None
    is_pattern: bool
    policy: BackpressurePolicy
    _handler: Handler
    _bus: TypedEventBus

    def unsubscribe(self) -> None:
        """Remove this subscription from its bus. Idempotent."""
        self._bus._unsubscribe(self)

    def __hash__(self) -> int:  # frozen dataclass hash by id
        return hash(self.id)


# ---------------------------------------------------------------------------
# Internal mirror (mutable per-subscription state)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SubEntry:
    """Bus-internal mirror of a :class:`Subscription`.

    Keeps mutable state (``busy``) off the public frozen dataclass so
    subscribers can compare / hash the handle without caring about
    runtime flags.
    """

    sub: Subscription
    busy: bool = False


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------


class TypedEventBus:
    """In-memory pub/sub over typed :class:`SignalEvent` values.

    Parameters
    ----------
    queue_maxlen:
        Size of the bounded in-memory event deque. Dropped-oldest
        backpressure. Default = :data:`DEFAULT_QUEUE_MAXLEN`.
    drop_warn_interval_seconds:
        Rate-limit for the drop-oldest WARNING log line. Pass ``0`` to
        log every drop (noisy).

    Examples
    --------

    Subscribe to a specific event type::

        bus = TypedEventBus()
        def handler(event: ToolCallEvent) -> None:
            print(event.tool_name)
        sub = bus.subscribe("tool_call", handler)
        bus.publish(ToolCallEvent(tool_name="Read", duration_seconds=0.1))
        sub.unsubscribe()

    Subscribe to every event (wildcard)::

        bus.subscribe(None, audit_logger)

    Subscribe with a glob pattern::

        bus.subscribe_pattern("web_*", web_sink)
    """

    def __init__(
        self,
        queue_maxlen: int = DEFAULT_QUEUE_MAXLEN,
        drop_warn_interval_seconds: float = DEFAULT_DROP_WARN_INTERVAL_SECONDS,
    ) -> None:
        self._queue_maxlen = queue_maxlen
        self._drop_warn_interval = drop_warn_interval_seconds
        self._lock = threading.Lock()
        self._subs: list[_SubEntry] = []
        # Bounded deque — drop-oldest accrues dropped_count so the caller
        # can reason about lost events without having to instrument
        # every subscriber. The deque is written inside the lock.
        self._queue: deque[SignalEvent] = deque(maxlen=queue_maxlen)
        self._dropped_count: int = 0
        self._last_drop_warn_at: float = 0.0

    # ─── subscription API ──────────────────────────────────────────

    def subscribe(
        self,
        event_type: str | None,
        handler: Handler,
        policy: BackpressurePolicy = BackpressurePolicy.BLOCK,
    ) -> Subscription:
        """Register ``handler`` for ``event_type`` (or all events).

        * ``event_type=None`` — wildcard; the handler receives EVERY
          published event regardless of type.
        * ``event_type="tool_call"`` — exact-match on the event
          discriminator.

        The returned :class:`Subscription` stays valid until
        :meth:`Subscription.unsubscribe` is called (idempotent).
        """
        return self._subscribe(
            event_type=event_type,
            is_pattern=False,
            handler=handler,
            policy=policy,
        )

    def subscribe_pattern(
        self,
        pattern: str,
        handler: Handler,
        policy: BackpressurePolicy = BackpressurePolicy.BLOCK,
    ) -> Subscription:
        """Register ``handler`` for every event whose ``event_type``
        matches the given :mod:`fnmatch` pattern.

        ``"*"`` matches everything. ``"web_*"`` matches
        ``"web_observation"`` (and anything else starting with
        ``web_``). ``"*_observation"`` matches
        ``"web_observation"`` and ``"file_observation"``.
        """
        return self._subscribe(
            event_type=pattern,
            is_pattern=True,
            handler=handler,
            policy=policy,
        )

    def _subscribe(
        self,
        *,
        event_type: str | None,
        is_pattern: bool,
        handler: Handler,
        policy: BackpressurePolicy,
    ) -> Subscription:
        sub = Subscription(
            id=str(uuid.uuid4()),
            event_type=event_type,
            is_pattern=is_pattern,
            policy=policy,
            _handler=handler,
            _bus=self,
        )
        entry = _SubEntry(sub=sub)
        with self._lock:
            self._subs.append(entry)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subs = [e for e in self._subs if e.sub.id != sub.id]

    def subscribers(self, event_type: str | None = None) -> list[Subscription]:
        """Return the subscriptions that would receive ``event_type``.

        If ``event_type`` is ``None``, return every subscription.
        Useful for debugging / introspection; does NOT publish.
        """
        with self._lock:
            snapshot = list(self._subs)
        if event_type is None:
            return [e.sub for e in snapshot]
        return [
            e.sub for e in snapshot if self._matches(e.sub, event_type)
        ]

    # ─── publish (sync) ────────────────────────────────────────────

    def publish(self, event: SignalEvent) -> str:
        """Fan out ``event`` to every matching sync subscriber.

        Async handlers attached via :meth:`subscribe` / :meth:`subscribe_pattern`
        are SKIPPED here — use :meth:`apublish` to await them. This
        keeps the sync path fast: publishers don't have to care about
        whether subscribers are coroutines.

        Returns
        -------
        str
            The ``event.event_id`` (convenience for logging / tracing).
        """
        self._enqueue(event)
        snapshot = self._matching_entries(event)
        for entry in snapshot:
            sub = entry.sub
            handler = sub._handler
            try:
                result = handler(event)
            except Exception:  # noqa: BLE001 — bus reliability
                _log.warning(
                    "bus: subscriber id=%s event_type=%s raised; continuing",
                    sub.id,
                    event.event_type,
                    exc_info=True,
                )
                continue
            if inspect.isawaitable(result):
                # Sync publish path: can't await. Best-effort close so
                # we don't dangle a RuntimeWarning. Callers that want
                # async semantics should use apublish.
                self._close_unawaited(result, sub)
        return event.event_id

    async def apublish(self, event: SignalEvent) -> str:
        """Fan out ``event`` concurrently to every matching subscriber.

        Sync handlers are called inline. Async handlers are awaited
        concurrently via :func:`asyncio.gather` so two slow
        subscribers don't serialize. Each handler is wrapped in its
        own try/except — one crash does not abort the others.
        """
        self._enqueue(event)
        snapshot = self._matching_entries(event)
        awaitables: list[Awaitable[Any]] = []
        for entry in snapshot:
            sub = entry.sub
            handler = sub._handler
            try:
                result = handler(event)
            except Exception:  # noqa: BLE001
                _log.warning(
                    "bus: subscriber id=%s event_type=%s raised; continuing",
                    sub.id,
                    event.event_type,
                    exc_info=True,
                )
                continue
            if inspect.isawaitable(result):
                awaitables.append(self._await_one(entry, result, event))
        if awaitables:
            await asyncio.gather(*awaitables, return_exceptions=False)
        return event.event_id

    async def _await_one(
        self,
        entry: _SubEntry,
        result: Awaitable[Any],
        event: SignalEvent,
    ) -> None:
        """Await a single async handler, respecting backpressure policy.

        * BLOCK: always await.
        * DROP: if handler is already running (``busy``), discard silently.
        * LOG_AND_DROP: same as DROP but log a WARNING.

        Exception isolation applies throughout — the gather layer in
        :meth:`apublish` uses ``return_exceptions=False`` but this
        wrapper swallows subscriber exceptions so nothing propagates
        back.
        """
        sub = entry.sub
        if sub.policy in (BackpressurePolicy.DROP, BackpressurePolicy.LOG_AND_DROP):
            if entry.busy:
                if sub.policy == BackpressurePolicy.LOG_AND_DROP:
                    _log.warning(
                        "bus: subscriber id=%s busy — dropping event id=%s type=%s",
                        sub.id,
                        event.event_id,
                        event.event_type,
                    )
                # close the coroutine so we don't emit a RuntimeWarning
                self._close_unawaited(result, sub)
                return
            entry.busy = True
            try:
                await result
            except Exception:  # noqa: BLE001
                _log.warning(
                    "bus: async subscriber id=%s raised; continuing",
                    sub.id,
                    exc_info=True,
                )
            finally:
                entry.busy = False
        else:  # BLOCK
            try:
                await result
            except Exception:  # noqa: BLE001
                _log.warning(
                    "bus: async subscriber id=%s raised; continuing",
                    sub.id,
                    exc_info=True,
                )

    @staticmethod
    def _close_unawaited(result: Any, sub: Subscription) -> None:
        """Close a coroutine we're not going to await.

        Prevents Python's ``RuntimeWarning: coroutine '...' was never
        awaited`` noise. If ``result`` is not actually a coroutine
        (e.g. some other awaitable), we silently skip.
        """
        close = getattr(result, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                _log.debug(
                    "bus: close() on unawaited result from subscriber id=%s "
                    "raised (non-fatal)",
                    sub.id,
                    exc_info=True,
                )

    # ─── queue / backpressure ──────────────────────────────────────

    def _enqueue(self, event: SignalEvent) -> None:
        """Append to the bounded queue, tracking dropped-oldest."""
        with self._lock:
            if len(self._queue) == self._queue_maxlen:
                # deque's maxlen will pop-left on append; record the
                # drop here BEFORE that happens so observers see the
                # count increment even if they only read dropped_count
                # after a burst.
                self._dropped_count += 1
                now = time.monotonic()
                if now - self._last_drop_warn_at >= self._drop_warn_interval:
                    _log.warning(
                        "bus: queue full (maxlen=%d) — dropping oldest; "
                        "total dropped=%d",
                        self._queue_maxlen,
                        self._dropped_count,
                    )
                    self._last_drop_warn_at = now
            self._queue.append(event)

    @property
    def dropped_count(self) -> int:
        """Total events dropped due to queue overflow since construction."""
        with self._lock:
            return self._dropped_count

    @property
    def queue_size(self) -> int:
        """Current queue depth (debug visibility only)."""
        with self._lock:
            return len(self._queue)

    def recent_events(self, limit: int = 100) -> list[SignalEvent]:
        """Return up to ``limit`` most-recent events for debugging.

        NOT intended as a durable replay mechanism — the queue is
        drop-oldest and has no persistence.
        """
        with self._lock:
            snapshot = list(self._queue)
        return snapshot[-limit:] if limit < len(snapshot) else snapshot

    # ─── matching helpers ──────────────────────────────────────────

    def _matching_entries(self, event: SignalEvent) -> list[_SubEntry]:
        """Snapshot subscribers that match this event's type."""
        with self._lock:
            # Copy under the lock so a concurrent unsubscribe can't
            # mutate the list while we iterate.
            snapshot = list(self._subs)
        return [e for e in snapshot if self._matches(e.sub, event.event_type)]

    @staticmethod
    def _matches(sub: Subscription, event_type: str) -> bool:
        """Return True if ``sub`` should receive ``event_type``."""
        if sub.event_type is None:
            return True
        if sub.is_pattern:
            return fnmatch.fnmatchcase(event_type, sub.event_type)
        return sub.event_type == event_type


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_default_bus_lock = threading.Lock()
default_bus: TypedEventBus = TypedEventBus()
"""Module-level shared bus — the simplest way to publish / subscribe.

Session B's B3 trajectory subscriber attaches here::

    from opencomputer.ingestion.bus import default_bus
    default_bus.subscribe("tool_call", my_trajectory_recorder)

Per the parallel-session protocol, breaking changes to this singleton's
public API must be announced in ``docs/parallel-sessions.md`` under
"Bus API change log".
"""


def get_default_bus() -> TypedEventBus:
    """Return the module-level :data:`default_bus` singleton.

    Exists alongside the plain module attribute for testability:
    tests that need a fresh bus instance can call
    :func:`reset_default_bus` and re-fetch via this function, while
    production code can just import ``default_bus`` directly.
    """
    return default_bus


def reset_default_bus() -> TypedEventBus:
    """Replace :data:`default_bus` with a fresh instance. Test-only.

    Every production caller SHOULD use the shared singleton — this
    function exists so that pytest fixtures can isolate state between
    tests without having to thread a bus through every callsite.
    """
    global default_bus
    with _default_bus_lock:
        default_bus = TypedEventBus()
    return default_bus


__all__ = [
    "BackpressurePolicy",
    "DEFAULT_QUEUE_MAXLEN",
    "DEFAULT_DROP_WARN_INTERVAL_SECONDS",
    "Handler",
    "Subscription",
    "TypedEventBus",
    "default_bus",
    "get_default_bus",
    "reset_default_bus",
]
