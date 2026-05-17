"""Pattern registry + F2 SignalEvent bus subscription wiring.

The registry is constructed once at AgentLoop init. It subscribes to the
default bus; every published SignalEvent is dispatched to every pattern.
Pattern firings are appended to an in-memory queue that the chat surfacer
drains at the start of each turn.

On construction the registry also loads the persisted muted-pattern set
from ``<profile-home>/awareness/muted_patterns.json`` — the same file
``oc awareness patterns mute`` writes (see ``cli_awareness.py``). That CLI
runs in a separate process, so without this load a freshly-started agent
process would always start with an empty ``_muted`` set and never honour a
mute set in a prior CLI invocation, breaking the spec promise that "a muted
pattern produces no hint, no cron".
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

from opencomputer.awareness.life_events.burnout import Burnout
from opencomputer.awareness.life_events.exam_prep import ExamPrep
from opencomputer.awareness.life_events.health_event import HealthEvent
from opencomputer.awareness.life_events.job_change import JobChange
from opencomputer.awareness.life_events.pattern import (
    LifeEventPattern,
    PatternFiring,
)
from opencomputer.awareness.life_events.relationship_shift import RelationshipShift
from opencomputer.awareness.life_events.travel import Travel

_log = logging.getLogger("opencomputer.awareness.life_events")

DEFAULT_PATTERNS: tuple[type[LifeEventPattern], ...] = (
    JobChange, ExamPrep, Burnout, RelationshipShift, HealthEvent, Travel,
)


def _muted_patterns_path() -> Path:
    """Return the path to the persisted muted-patterns JSON list.

    ``<profile-home>/awareness/muted_patterns.json`` — the exact file
    ``oc awareness patterns {mute,unmute}`` writes (``cli_awareness.py``).
    Profile home is resolved through ``opencomputer.agent.config._home``,
    the canonical core resolver, the same way ``state.py`` does it — so an
    ``awareness/`` module never has to import a ``cli_*`` module (that
    would be backwards layering). Resolved per call (not cached) so a
    per-test ``OPENCOMPUTER_HOME`` monkey-patch picks up the right tmp path.
    """
    from opencomputer.agent.config import _home

    return _home() / "awareness" / "muted_patterns.json"


def _load_persisted_muted() -> set[str]:
    """Load the persisted muted pattern IDs as a set. Tolerates everything.

    Returns an empty set — never raises — when the file is absent,
    unreadable, not valid JSON, or valid JSON that is not a list. Matches
    the JSON shape (a plain list of strings) that ``cli_awareness._save_muted``
    writes and ``cli_awareness._load_muted`` reads.
    """
    path = _muted_patterns_path()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning(
            "muted_patterns.json read failed: %s; treating as no mutes", exc
        )
        return set()
    if not isinstance(data, list):
        _log.warning(
            "muted_patterns.json is not a list (got %s); treating as no mutes",
            type(data).__name__,
        )
        return set()
    return {str(x) for x in data}


class LifeEventRegistry:
    """Owns the pattern instances + firing queue."""

    def __init__(self, patterns: list[LifeEventPattern] | None = None) -> None:
        if patterns is None:
            patterns = [cls() for cls in DEFAULT_PATTERNS]
        self._patterns: dict[str, LifeEventPattern] = {p.pattern_id: p for p in patterns}
        # Seed the muted set from the persisted file written by
        # ``oc awareness patterns mute`` (a separate process). Without this
        # a fresh agent process would start mute-free and ignore an earlier
        # CLI mute — breaking the "muted = no hint, no cron" promise. The
        # loader tolerates a missing/corrupt file (→ empty set), so this
        # never breaks registry construction.
        self._muted: set[str] = _load_persisted_muted()
        self._queue: list[PatternFiring] = []
        # Latest queued firing, retained independently of the queue so that
        # peek_most_recent_firing survives drain_pending (see that method).
        self._last_firing: PatternFiring | None = None

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
            self._last_firing = firing

    def drain_pending(self) -> list[PatternFiring]:
        """Pop all queued firings (called by chat surfacer at turn start)."""
        out, self._queue = self._queue, []
        return out

    def peek_most_recent_firing(self) -> PatternFiring | None:
        """Return the most-recently-recorded non-silent firing.

        Returns ``_last_firing`` — the LAST firing appended to the queue,
        i.e. the most recently recorded one — NOT a max-by-timestamp scan.
        ``_last_firing`` is reassigned on every ``_queue.append`` (in event
        arrival order), so it is whichever non-silent firing was recorded
        last.

        Path A.3 (2026-04-27): the companion-persona overlay augmentation
        wants to read the freshest firing as anchor context for the LLM.

        The firing is also legitimate input for the chat surfacer / injection
        provider, which calls ``drain_pending`` at the start of each turn.
        Reading from ``_queue`` here would mean peek returns ``None`` for the
        rest of the turn after a drain. ``_last_firing`` is updated alongside
        every ``_queue.append`` and is never cleared by ``drain_pending``, so
        peeking stays non-destructive AND survives a drain.

        Returns None until the first non-silent firing is queued. Silent
        firings are not retained — they never reach this method, matching
        their exclusion from the chat-surfacer queue.
        """
        return self._last_firing


# ── Module-level singleton (Path A.3) ─────────────────────────────────

_GLOBAL_REGISTRY: LifeEventRegistry | None = None
_GLOBAL_BUS_UNSUB: Callable[[], None] | None = None


def get_global_registry() -> LifeEventRegistry:
    """Return the process-wide singleton, creating it on first access.

    Path A.3 (2026-04-27): the companion-persona overlay reads the
    most-recent firing as context. Building per-AgentLoop registries
    would mean the bus subscription multiplies; a singleton matches the
    one-bus-per-process pattern used elsewhere in OpenComputer.

    Subscription to the F2 bus happens lazily on first access — if the
    bus isn't yet initialized (early in startup, tests), the subscription
    is silently skipped and will be retried on the next call. Errors
    during subscription never propagate; the persona overlay degrades to
    "no firing" and the chat loop is unaffected.
    """
    global _GLOBAL_REGISTRY, _GLOBAL_BUS_UNSUB
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = LifeEventRegistry()
    if _GLOBAL_BUS_UNSUB is None:
        try:
            from opencomputer.ingestion.bus import get_default_bus

            bus = get_default_bus()
            if bus is not None:
                _GLOBAL_BUS_UNSUB = subscribe_to_bus(_GLOBAL_REGISTRY, bus)
        except Exception:
            _log.debug("global registry bus subscribe deferred", exc_info=True)
    return _GLOBAL_REGISTRY


def reset_global_registry_for_test() -> None:
    """Test helper — drop the singleton so each test gets a fresh one."""
    global _GLOBAL_REGISTRY, _GLOBAL_BUS_UNSUB
    if _GLOBAL_BUS_UNSUB is not None:
        try:
            _GLOBAL_BUS_UNSUB()
        except Exception:
            pass
    _GLOBAL_REGISTRY = None
    _GLOBAL_BUS_UNSUB = None


def subscribe_to_bus(registry: LifeEventRegistry, bus) -> Callable[[], None]:
    """Wire registry to the F2 SignalEvent bus. Returns an unsubscribe callable."""
    def handler(event):
        # event is a SignalEvent; extract event_type + metadata
        registry.on_event(event.event_type, dict(event.metadata))
    sub = bus.subscribe_pattern("*", handler)  # all events
    return lambda: sub.unsubscribe()
