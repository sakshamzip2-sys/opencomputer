"""Inbound queue manager — replaces the per-(profile,session) asyncio.Lock.

S1 from 2026-05-06 OpenClaw deep-comparison brief. The manager preserves
the historical ``followup`` behavior as default and supports four modes:

* ``followup`` (default) — serialized run-to-completion (legacy behavior)
* ``interrupt`` — cancel any in-flight run, then start the new one
* ``collect`` — buffer incoming messages within a debounce window; drain
  the buffer once into a single agent run when the window closes
* ``steer`` — alias for interrupt (placeholder for full replan-with-context
  port that needs agent-loop coordination; behaves like interrupt today)

Drop policy applies when the ``collect`` buffer reaches ``collect_cap``:

* ``drop_old`` — discard oldest queued message
* ``drop_new`` — discard the new message, keep the buffer
* ``summarize`` — replace queued messages with one summary line
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from plugin_sdk.queue import (
    ALL_DROP_POLICIES,
    ALL_QUEUE_MODES,
    DEFAULT_COLLECT_CAP,
    DEFAULT_COLLECT_DEBOUNCE_S,
    DEFAULT_DROP_POLICY,
    DEFAULT_QUEUE_MODE,
    DropPolicy,
    QueueConfig,
    QueueMode,
)

logger = logging.getLogger("opencomputer.gateway.queue_manager")


@dataclass
class _SlotState:
    """Per-(profile,session) state held by QueueManager."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The currently running dispatch task. ``None`` when nothing is in flight.
    in_flight: asyncio.Task | None = None


@dataclass
class _CollectState:
    """Per-session collect-mode buffer state."""

    buffer: list[str] = field(default_factory=list)
    #: Set when the latest debounce timer fires; cleared when the drain
    #: callback acquires it. Drain coalesces concurrent producers.
    drain_event: asyncio.Event = field(default_factory=asyncio.Event)
    #: Active timer task — cancelled and replaced on each new message.
    timer: asyncio.Task | None = None
    #: Counts how many messages this drain represents (for telemetry).
    drained_count: int = 0


class QueueManager:
    """Single source of truth for inbound queue behavior across all chats."""

    def __init__(
        self,
        *,
        default_mode: QueueMode = DEFAULT_QUEUE_MODE,
        default_collect_debounce_s: float = DEFAULT_COLLECT_DEBOUNCE_S,
        default_collect_cap: int = DEFAULT_COLLECT_CAP,
        default_drop_policy: DropPolicy = DEFAULT_DROP_POLICY,
    ) -> None:
        if default_mode not in ALL_QUEUE_MODES:
            raise ValueError(
                f"unknown queue mode {default_mode!r}; "
                f"valid: {ALL_QUEUE_MODES}"
            )
        if default_drop_policy not in ALL_DROP_POLICIES:
            raise ValueError(
                f"unknown drop policy {default_drop_policy!r}; "
                f"valid: {ALL_DROP_POLICIES}"
            )
        self._default_mode: QueueMode = default_mode
        self._default_collect_debounce_s: float = default_collect_debounce_s
        self._default_collect_cap: int = default_collect_cap
        self._default_drop_policy: DropPolicy = default_drop_policy
        self._slots: dict[tuple[str, str], _SlotState] = {}
        # Per-session override (key=session_id, value=mode). Slash command writes here.
        self._session_modes: dict[str, QueueMode] = {}
        # Per-session config overrides (debounce/cap/drop_policy).
        self._session_configs: dict[str, QueueConfig] = {}
        # Per-session collect-mode buffers.
        self._collect: dict[str, _CollectState] = {}

    @property
    def default_mode(self) -> QueueMode:
        return self._default_mode

    def set_default_mode(self, mode: QueueMode) -> None:
        if mode not in ALL_QUEUE_MODES:
            raise ValueError(f"unknown queue mode {mode!r}")
        self._default_mode = mode

    def get_session_mode(self, session_id: str) -> QueueMode:
        """Resolve the active mode for ``session_id`` (override → default)."""
        return self._session_modes.get(session_id, self._default_mode)

    def has_session_mode(self, session_id: str) -> bool:
        """True if ``session_id`` has an explicit per-session override.

        A9 — the gateway seeds a binding's ``queue_mode`` exactly once
        per session (only when no override exists yet) so a later
        ``/queue-mode`` from the user is never clobbered.
        """
        return session_id in self._session_modes

    def set_session_mode(self, session_id: str, mode: QueueMode) -> None:
        if mode not in ALL_QUEUE_MODES:
            raise ValueError(f"unknown queue mode {mode!r}")
        self._session_modes[session_id] = mode

    def clear_session_mode(self, session_id: str) -> None:
        self._session_modes.pop(session_id, None)

    def get_session_config(self, session_id: str) -> QueueConfig:
        if session_id in self._session_configs:
            return self._session_configs[session_id]
        return QueueConfig(
            mode=self.get_session_mode(session_id),
            collect_debounce_s=self._default_collect_debounce_s,
            collect_cap=self._default_collect_cap,
            drop_policy=self._default_drop_policy,
        )

    def set_session_config(self, session_id: str, cfg: QueueConfig) -> None:
        if cfg.mode not in ALL_QUEUE_MODES:
            raise ValueError(f"unknown queue mode {cfg.mode!r}")
        if cfg.drop_policy not in ALL_DROP_POLICIES:
            raise ValueError(f"unknown drop policy {cfg.drop_policy!r}")
        self._session_configs[session_id] = cfg
        self._session_modes[session_id] = cfg.mode

    def _slot(self, profile_id: str, session_id: str) -> _SlotState:
        key = (profile_id, session_id)
        if key not in self._slots:
            self._slots[key] = _SlotState()
        return self._slots[key]

    @asynccontextmanager
    async def acquire(self, profile_id: str, session_id: str):
        """Acquire the run slot for (profile_id, session_id).

        Behavior depends on the resolved mode:

        * ``followup`` — wait for any current run to finish, then proceed.
          (Same semantics as the legacy asyncio.Lock.)
        * ``interrupt`` — cancel any current run, then proceed immediately.
        * ``collect`` — fall through to the same lock as ``followup``;
          the dispatcher should call ``buffer_message`` first and use
          ``await drained()`` to know when to invoke the agent. ``acquire``
          remains the serialization primitive once the buffer drains.
        * ``steer`` — currently aliases ``interrupt`` (cancel + restart).
          A future replan-with-context port can override this branch.
        """
        slot = self._slot(profile_id, session_id)
        mode = self.get_session_mode(session_id)

        if mode in ("interrupt", "steer"):
            existing = slot.in_flight
            if existing is not None and not existing.done():
                existing.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await existing
        # Always serialize through the per-slot lock — even interrupt mode
        # needs serialized access to the dispatch path so two interrupts
        # don't race the cancel/restart sequence.
        await slot.lock.acquire()
        try:
            current = asyncio.current_task()
            slot.in_flight = current
            try:
                yield
            finally:
                if slot.in_flight is current:
                    slot.in_flight = None
        finally:
            slot.lock.release()

    # ─── collect mode ──────────────────────────────────────────────────

    def buffer_message(self, session_id: str, text: str) -> bool:
        """Add a message to the collect-mode buffer for ``session_id``.

        Applies the configured drop policy when the buffer is full.
        Returns ``True`` if the message was buffered, ``False`` if it
        was dropped under ``drop_new`` policy.
        """
        cfg = self.get_session_config(session_id)
        state = self._collect.setdefault(session_id, _CollectState())

        if len(state.buffer) >= cfg.collect_cap:
            if cfg.drop_policy == "drop_new":
                return False
            if cfg.drop_policy == "drop_old":
                state.buffer.pop(0)
            elif cfg.drop_policy == "summarize":
                # Replace the entire buffer with a single summary line.
                summary = (
                    f"[{len(state.buffer)} earlier messages summarised "
                    "due to overflow]"
                )
                state.buffer = [summary]

        state.buffer.append(text)
        return True

    def buffered(self, session_id: str) -> list[str]:
        state = self._collect.get(session_id)
        if state is None:
            return []
        return list(state.buffer)

    def drain_buffer(self, session_id: str) -> str:
        """Pop the entire buffer and return one merged user-text string.

        Newlines join sequential messages so the agent sees them as a
        coherent stream. Empty buffer → empty string.
        """
        state = self._collect.get(session_id)
        if state is None or not state.buffer:
            return ""
        text = "\n".join(state.buffer)
        state.drained_count += len(state.buffer)
        state.buffer = []
        return text

    async def schedule_collect_drain(self, session_id: str) -> None:
        """Restart the debounce timer; on expiry, set the drain event.

        Producers call this each time a new message arrives in collect
        mode. Consumers ``await wait_for_drain(session_id)`` to block
        until the timer fires.
        """
        cfg = self.get_session_config(session_id)
        state = self._collect.setdefault(session_id, _CollectState())

        if state.timer is not None and not state.timer.done():
            state.timer.cancel()

        async def _expire(delay: float, event: asyncio.Event) -> None:
            try:
                await asyncio.sleep(delay)
                event.set()
            except asyncio.CancelledError:
                # Cancellation is the normal "another message arrived" path.
                # Don't propagate.
                return

        state.drain_event.clear()
        state.timer = asyncio.create_task(
            _expire(cfg.collect_debounce_s, state.drain_event)
        )

    async def wait_for_drain(self, session_id: str) -> None:
        """Block until the debounce timer fires for ``session_id``."""
        state = self._collect.setdefault(session_id, _CollectState())
        await state.drain_event.wait()


_ACTIVE_MANAGER: QueueManager | None = None


def set_active_manager(manager: QueueManager | None) -> None:
    """Register the gateway's QueueManager so the /queue-mode slash command can find it."""
    global _ACTIVE_MANAGER
    _ACTIVE_MANAGER = manager


def get_active_manager() -> QueueManager | None:
    """Return the currently registered QueueManager, or None when no gateway is active."""
    return _ACTIVE_MANAGER


__all__ = [
    "QueueManager",
    "get_active_manager",
    "set_active_manager",
]
