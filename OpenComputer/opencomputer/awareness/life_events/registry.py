"""Pattern registry + F2 SignalEvent bus subscription wiring.

The registry is constructed once at AgentLoop init. It subscribes to the
default bus; every published SignalEvent is dispatched to every pattern.
Pattern firings are appended to an in-memory queue that the chat surfacer
drains at the start of each turn.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from opencomputer.awareness.life_events.burnout import Burnout
from opencomputer.awareness.life_events.exam_prep import ExamPrep
from opencomputer.awareness.life_events.health_event import HealthEvent
from opencomputer.awareness.life_events.job_change import JobChange
from opencomputer.awareness.life_events.pattern import (
    LifeEventPattern, PatternFiring,
)
from opencomputer.awareness.life_events.relationship_shift import RelationshipShift
from opencomputer.awareness.life_events.travel import Travel

_log = logging.getLogger("opencomputer.awareness.life_events")

DEFAULT_PATTERNS: tuple[type[LifeEventPattern], ...] = (
    JobChange, ExamPrep, Burnout, RelationshipShift, HealthEvent, Travel,
)


class LifeEventRegistry:
    """Owns the pattern instances + firing queue."""

    def __init__(self, patterns: list[LifeEventPattern] | None = None) -> None:
        if patterns is None:
            patterns = [cls() for cls in DEFAULT_PATTERNS]
        self._patterns: dict[str, LifeEventPattern] = {p.pattern_id: p for p in patterns}
        self._muted: set[str] = set()
        self._queue: list[PatternFiring] = []

    def is_muted(self, pattern_id: str) -> bool:
        return pattern_id in self._muted

    def mute(self, pattern_id: str) -> None:
        self._muted.add(pattern_id)

    def unmute(self, pattern_id: str) -> None:
        self._muted.discard(pattern_id)

    def list_patterns(self) -> list[tuple[str, str, bool]]:
        """Return [(pattern_id, surfacing, muted)]."""
        return [
            (p.pattern_id, p.surfacing, p.pattern_id in self._muted)
            for p in self._patterns.values()
        ]

    def on_event(self, event_type: str, metadata: dict[str, object]) -> None:
        """Bus subscription handler — dispatch event to every non-muted pattern."""
        for pattern_id, pattern in self._patterns.items():
            if pattern_id in self._muted:
                continue
            try:
                firing = pattern.accumulate(event_type, metadata)
            except Exception:  # noqa: BLE001
                _log.exception("Pattern %s.accumulate raised", pattern_id)
                continue
            if firing is None:
                continue
            # Silent firings still go to F4 graph but aren't queued for chat
            if firing.surfacing == "silent":
                _log.debug("Silent firing %s confidence=%.2f", pattern_id, firing.confidence)
                continue
            self._queue.append(firing)

    def drain_pending(self) -> list[PatternFiring]:
        """Pop all queued firings (called by chat surfacer at turn start)."""
        out, self._queue = self._queue, []
        return out


def subscribe_to_bus(registry: LifeEventRegistry, bus) -> Callable[[], None]:
    """Wire registry to the F2 SignalEvent bus. Returns an unsubscribe callable."""
    def handler(event):
        # event is a SignalEvent; extract event_type + metadata
        registry.on_event(event.event_type, dict(event.metadata))
    sub = bus.subscribe_pattern("*", handler)  # all events
    return lambda: sub.unsubscribe()
