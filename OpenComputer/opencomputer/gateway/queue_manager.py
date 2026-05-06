"""Inbound queue manager — replaces the per-(profile,session) asyncio.Lock.

Phase 2 (S1 from 2026-05-06 OpenClaw deep-comparison brief). The
manager preserves the historical ``followup`` behavior as default and
adds ``interrupt`` mode that cancels any in-flight run for the same key
before starting a new one.

Design notes:

* Per-key state lives on the manager instance. The gateway holds a
  single QueueManager; ``Dispatch.handle_message`` calls
  ``async with manager.acquire(profile_id, session_id)`` instead of
  acquiring a raw lock.
* ``acquire`` registers an ``asyncio.Task`` reference for the active
  run. ``interrupt`` mode cancels the previous task (if any) before
  the new one starts.
* Mode resolution is per-(profile, session) with a manager-level
  default. Slash commands set per-session overrides via
  ``set_session_mode``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from plugin_sdk.queue import (
    ALL_QUEUE_MODES,
    DEFAULT_QUEUE_MODE,
    QueueMode,
)

logger = logging.getLogger("opencomputer.gateway.queue_manager")


@dataclass
class _SlotState:
    """Per-(profile,session) state held by QueueManager."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The currently running dispatch task. ``None`` when nothing is in flight.
    in_flight: asyncio.Task | None = None


class QueueManager:
    """Single source of truth for inbound queue behavior across all chats."""

    def __init__(self, *, default_mode: QueueMode = DEFAULT_QUEUE_MODE) -> None:
        if default_mode not in ALL_QUEUE_MODES:
            raise ValueError(
                f"unknown queue mode {default_mode!r}; "
                f"valid: {ALL_QUEUE_MODES}"
            )
        self._default_mode: QueueMode = default_mode
        self._slots: dict[tuple[str, str], _SlotState] = {}
        # Per-session override (key=session_id, value=mode). Slash command writes here.
        self._session_modes: dict[str, QueueMode] = {}

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

    def set_session_mode(self, session_id: str, mode: QueueMode) -> None:
        if mode not in ALL_QUEUE_MODES:
            raise ValueError(f"unknown queue mode {mode!r}")
        self._session_modes[session_id] = mode

    def clear_session_mode(self, session_id: str) -> None:
        self._session_modes.pop(session_id, None)

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
        """
        slot = self._slot(profile_id, session_id)
        mode = self.get_session_mode(session_id)

        if mode == "interrupt":
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
