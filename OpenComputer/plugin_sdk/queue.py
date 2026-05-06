"""Inbound queue mode primitives — Phase 2 (S1 from 2026-05-06 OpenClaw deep-comparison).

When a new message arrives while the gateway is mid-reply, the queue
mode determines what happens:

* ``followup`` (default) — finish current reply, then handle the new
  message. This is the historical behavior (per-(profile,session)
  asyncio.Lock).
* ``interrupt`` — cancel the current run and start over with the new
  message. Useful for "wait, also do X" mid-reply patterns.

Modes deferred to a future iteration: ``collect`` (buffer until idle),
``steer`` (abort + replan with merged context), ``steer-backlog``,
debounce + cap + drop policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

QueueMode = Literal["followup", "interrupt"]

#: All supported modes — useful for slash-command validation.
ALL_QUEUE_MODES: tuple[QueueMode, ...] = ("followup", "interrupt")

DEFAULT_QUEUE_MODE: QueueMode = "followup"


@dataclass(frozen=True, slots=True)
class QueueConfig:
    """Per-session queue configuration."""

    mode: QueueMode = DEFAULT_QUEUE_MODE


__all__ = [
    "ALL_QUEUE_MODES",
    "DEFAULT_QUEUE_MODE",
    "QueueConfig",
    "QueueMode",
]
