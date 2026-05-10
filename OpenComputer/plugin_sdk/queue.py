"""Inbound queue mode primitives — S1 from 2026-05-06 OpenClaw deep-comparison.

When a new message arrives while the gateway is mid-reply, the queue
mode determines what happens:

* ``followup`` (default) — finish current reply, then handle the new
  message. This is the historical behavior (per-(profile,session)
  asyncio.Lock).
* ``interrupt`` — cancel the current run and start over with the new
  message. Useful for "wait, also do X" mid-reply patterns.
* ``collect`` — buffer messages within a debounce window; when the
  window closes, run the agent ONCE with the merged text. Best for
  rapid-fire user typing where each message is a fragment of one thought.
* ``steer`` — alias for ``interrupt`` today. Reserved for a future
  full-port that replans the in-flight run with merged context (today's
  ``interrupt`` cancels + restarts cleanly, which is a sufficient
  approximation when the agent loop's resume-with-context isn't wired).

Drop policy (when the buffer cap fills in ``collect`` mode):
* ``drop_old`` — discard the oldest queued message
* ``drop_new`` — discard the new message, keep the queue
* ``summarize`` — replace queued messages with one summary line
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

QueueMode = Literal["followup", "interrupt", "collect", "steer"]

#: All supported modes — useful for slash-command validation.
ALL_QUEUE_MODES: tuple[QueueMode, ...] = (
    "followup",
    "interrupt",
    "collect",
    "steer",
)

DEFAULT_QUEUE_MODE: QueueMode = "followup"

DropPolicy = Literal["drop_old", "drop_new", "summarize"]

ALL_DROP_POLICIES: tuple[DropPolicy, ...] = (
    "drop_old",
    "drop_new",
    "summarize",
)

#: Default debounce window for ``collect`` mode (seconds).
DEFAULT_COLLECT_DEBOUNCE_S: float = 1.5

#: Default buffer cap for ``collect`` mode.
DEFAULT_COLLECT_CAP: int = 50

DEFAULT_DROP_POLICY: DropPolicy = "drop_old"


@dataclass(frozen=True, slots=True)
class QueueConfig:
    """Per-session queue configuration."""

    mode: QueueMode = DEFAULT_QUEUE_MODE
    #: Debounce window for ``collect`` mode — when no new message arrives
    #: within this many seconds, drain the buffer.
    collect_debounce_s: float = DEFAULT_COLLECT_DEBOUNCE_S
    #: Buffer cap for ``collect`` mode — drop policy applies on overflow.
    collect_cap: int = DEFAULT_COLLECT_CAP
    drop_policy: DropPolicy = DEFAULT_DROP_POLICY


__all__ = [
    "ALL_DROP_POLICIES",
    "ALL_QUEUE_MODES",
    "DEFAULT_COLLECT_CAP",
    "DEFAULT_COLLECT_DEBOUNCE_S",
    "DEFAULT_DROP_POLICY",
    "DEFAULT_QUEUE_MODE",
    "DropPolicy",
    "QueueConfig",
    "QueueMode",
]
