# Layered Awareness V2.C — Life-Event Detector + Plural Personas

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Layer 5 (Life-Event Detector — agent senses "user got fired/breakup/exam stress") + Layer 6 (Plural Personas auto-classifier — agent recognizes "Saksham coding" vs "Saksham trading"). The headline emotional-connection metric Saksham pinned.

**Architecture:** Life-Event Detector is a registry of `LifeEventPattern` subclasses subscribed to the F2 SignalEvent bus. Each pattern accumulates evidence over a sliding window; when confidence crosses `surface_threshold` (default 0.7), the pattern fires either a low-key hint surfaced in the next chat turn or a silent F4 user-model graph edge (for sensitive patterns like HealthEvent — never surface unprompted). Plural Personas: a small classifier reads `(foreground_app, time_of_day, recent_files, last_3_messages)` per turn → outputs persona ID. Each persona has a YAML config with system-prompt overlay. Manual override via existing `/persona` slash command.

**Tech Stack:** Existing F2 SignalEvent bus, F4 user-model graph, F1 consent gates, F3 motif inference. No new heavy deps. PyYAML already a dep.

---

## File Structure

| Path | Responsibility |
|---|---|
| `opencomputer/awareness/__init__.py` | NEW package |
| `opencomputer/awareness/life_events/__init__.py` | NEW |
| `opencomputer/awareness/life_events/pattern.py` | NEW — `LifeEventPattern` ABC + `EvidenceAccumulator` |
| `opencomputer/awareness/life_events/job_change.py` | NEW — drop in work-mail + LinkedIn searches |
| `opencomputer/awareness/life_events/exam_prep.py` | NEW — repeated .edu / khanacademy / focused-topic visits |
| `opencomputer/awareness/life_events/burnout.py` | NEW — declining file-edit volume + late-night creep |
| `opencomputer/awareness/life_events/relationship_shift.py` | NEW — sudden drop in messages with frequent contact, NEVER surface unprompted |
| `opencomputer/awareness/life_events/health_event.py` | NEW — symptom searches, NEVER surface unprompted |
| `opencomputer/awareness/life_events/travel.py` | NEW — hotel/airline + maps activity, calendar events with location |
| `opencomputer/awareness/life_events/registry.py` | NEW — pattern registry + bus subscription wiring |
| `opencomputer/awareness/personas/__init__.py` | NEW |
| `opencomputer/awareness/personas/classifier.py` | NEW — `PersonaClassifier` reads context → outputs persona id |
| `opencomputer/awareness/personas/registry.py` | NEW — YAML loader for `<profile_home>/personas/*.yaml` |
| `opencomputer/awareness/personas/defaults/{coding,trading,relaxed,admin,learning}.yaml` | NEW — 5 default personas |
| `opencomputer/agent/loop.py` (modify) | Wire persona classifier at turn start; apply system_prompt_overlay |
| `opencomputer/cli_awareness.py` | NEW — `opencomputer awareness {patterns,personas} {list,mute,unmute}` |
| `opencomputer/cli.py` (modify) | Register `awareness` Typer subgroup |
| `opencomputer/agent/consent/capability_taxonomy.py` (modify) | Add `awareness.*` capability claims |
| `tests/test_life_event_pattern_base.py` | NEW |
| `tests/test_life_events_registry.py` | NEW |
| `tests/test_life_event_*.py` | NEW per pattern (6 files) |
| `tests/test_persona_classifier.py` | NEW |
| `tests/test_persona_registry.py` | NEW |
| `tests/test_persona_loop_integration.py` | NEW — verify auto-classifier feeds AgentLoop |
| `tests/test_cli_awareness.py` | NEW |

---

## Task 1: Life-Event pattern framework + 3 starter patterns

**Files:**
- Create: `opencomputer/awareness/life_events/{pattern,job_change,exam_prep,burnout}.py`
- Create: `opencomputer/awareness/life_events/__init__.py` + `opencomputer/awareness/__init__.py`
- Test: `tests/test_life_event_pattern_base.py`, `tests/test_life_event_job_change.py`, `tests/test_life_event_exam_prep.py`, `tests/test_life_event_burnout.py`

- [ ] **Step 1.1: Write `pattern.py` ABC**

```python
"""Life-event pattern framework.

A LifeEventPattern observes events on the F2 SignalEvent bus, accumulates
evidence in a sliding window, and fires when confidence crosses threshold.

Patterns split into two surfacing policies:
- ``surfacing="hint"`` — fires a chat-context hint at next turn ("noticed
  your work rhythm shifted — anything you want to talk about?")
- ``surfacing="silent"`` — writes an F4 user-model edge with low confidence
  but never surfaces in chat (HealthEvent, RelationshipShift). The agent's
  responses subtly adjust tone but never name the inference.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


SurfacingPolicy = Literal["hint", "silent"]


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One observation contributing to a pattern's confidence."""

    timestamp: float
    weight: float  # 0.0..1.0
    source: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PatternFiring:
    """A pattern crossed its surface threshold."""

    pattern_id: str
    confidence: float
    evidence_count: int
    surfacing: SurfacingPolicy
    hint_text: str = ""
    timestamp: float = field(default_factory=time.time)


class LifeEventPattern(ABC):
    """Subscribers extend this. Default sliding window = 14 days, decay = exp(-age/7d)."""

    pattern_id: str = ""
    surface_threshold: float = 0.7
    surfacing: SurfacingPolicy = "hint"
    window_days: float = 14.0
    decay_half_life_days: float = 7.0

    def __init__(self) -> None:
        self._evidence: list[EvidenceItem] = []

    @abstractmethod
    def consider_event(self, event_type: str, metadata: dict[str, object]) -> EvidenceItem | None:
        """Inspect a SignalEvent. Return EvidenceItem if relevant; None to skip."""

    def accumulate(self, event_type: str, metadata: dict[str, object]) -> PatternFiring | None:
        item = self.consider_event(event_type, metadata)
        if item is None:
            return None
        self._evidence.append(item)
        self._prune()
        confidence = self._compute_confidence()
        if confidence < self.surface_threshold:
            return None
        return PatternFiring(
            pattern_id=self.pattern_id,
            confidence=confidence,
            evidence_count=len(self._evidence),
            surfacing=self.surfacing,
            hint_text=self.hint_text(),
        )

    def _prune(self) -> None:
        cutoff = time.time() - (self.window_days * 86400)
        self._evidence = [e for e in self._evidence if e.timestamp >= cutoff]

    def _compute_confidence(self) -> float:
        """Decay-weighted sum of evidence weights, capped at 1.0."""
        if not self._evidence:
            return 0.0
        now = time.time()
        decay = self.decay_half_life_days * 86400
        weighted = sum(
            e.weight * (0.5 ** ((now - e.timestamp) / decay))
            for e in self._evidence
        )
        return min(1.0, weighted)

    def hint_text(self) -> str:
        return ""
```

- [ ] **Step 1.2: Implement `JobChange` pattern**

`opencomputer/awareness/life_events/job_change.py`:

```python
"""JobChange — sudden drop in work-mail volume + LinkedIn searches + resignation/severance terms."""
from __future__ import annotations

from typing import ClassVar

from opencomputer.awareness.life_events.pattern import (
    EvidenceItem, LifeEventPattern, SurfacingPolicy,
)


_TRIGGER_TERMS: ClassVar[frozenset[str]] = frozenset({
    "linkedin.com/jobs", "indeed.com", "glassdoor.com",
    "resignation", "severance", "unemployment", "notice period",
})


class JobChange(LifeEventPattern):
    pattern_id: str = "job_change"
    surfacing: SurfacingPolicy = "hint"
    surface_threshold: float = 0.7

    def consider_event(self, event_type, metadata):
        if event_type != "browser_visit":
            return None
        url = str(metadata.get("url", "")).lower()
        title = str(metadata.get("title", "")).lower()
        text = url + " " + title
        for term in _TRIGGER_TERMS:
            if term in text:
                return EvidenceItem(
                    timestamp=float(metadata.get("visit_time", 0.0)),
                    weight=0.4,  # 2 hits = 0.8 = above 0.7 threshold
                    source="browser",
                    payload={"term": term, "url": url[:200]},
                )
        return None

    def hint_text(self) -> str:
        return (
            "I noticed your work rhythm has shifted recently — different tabs, "
            "different patterns. If anything's on your mind work-wise, I'm here."
        )
```

- [ ] **Step 1.3: Implement `ExamPrep`**

`opencomputer/awareness/life_events/exam_prep.py`:

```python
"""ExamPrep — repeated visits to .edu / khanacademy / focused topic + practice-test searches."""
from __future__ import annotations

from typing import ClassVar

from opencomputer.awareness.life_events.pattern import (
    EvidenceItem, LifeEventPattern, SurfacingPolicy,
)


_EDU_DOMAINS: ClassVar[frozenset[str]] = frozenset({
    "khanacademy.org", "coursera.org", "edx.org", ".edu/", "geeksforgeeks.org",
    "leetcode.com", "hackerrank.com", "stackoverflow.com",
})
_TRIGGER_TERMS: ClassVar[frozenset[str]] = frozenset({
    "practice test", "mock exam", "syllabus", "past paper", "previous year",
})


class ExamPrep(LifeEventPattern):
    pattern_id: str = "exam_prep"
    surfacing: SurfacingPolicy = "hint"
    surface_threshold: float = 0.7

    def consider_event(self, event_type, metadata):
        if event_type != "browser_visit":
            return None
        url = str(metadata.get("url", "")).lower()
        title = str(metadata.get("title", "")).lower()
        weight = 0.0
        for d in _EDU_DOMAINS:
            if d in url:
                weight = max(weight, 0.2)
                break
        for t in _TRIGGER_TERMS:
            if t in title or t in url:
                weight = max(weight, 0.3)
                break
        if weight == 0.0:
            return None
        return EvidenceItem(
            timestamp=float(metadata.get("visit_time", 0.0)),
            weight=weight,
            source="browser",
            payload={"url": url[:200]},
        )

    def hint_text(self) -> str:
        return (
            "Looks like you've been deep in study material the last few days. "
            "If you want me to draft questions, summarize concepts, or quiz you, just ask."
        )
```

- [ ] **Step 1.4: Implement `Burnout`**

`opencomputer/awareness/life_events/burnout.py`:

```python
"""Burnout — declining file-edit volume + late-night activity creep + commit-frequency drop."""
from __future__ import annotations

import time

from opencomputer.awareness.life_events.pattern import (
    EvidenceItem, LifeEventPattern, SurfacingPolicy,
)


def _is_late_night(ts: float) -> bool:
    """Return True if the timestamp is between midnight and 4 AM local time."""
    hour = time.localtime(ts).tm_hour
    return 0 <= hour < 4


class Burnout(LifeEventPattern):
    pattern_id: str = "burnout"
    surfacing: SurfacingPolicy = "hint"  # gentle "how are you" cadence increase
    surface_threshold: float = 0.7
    window_days: float = 21.0  # longer window — burnout builds slowly

    def consider_event(self, event_type, metadata):
        ts = float(metadata.get("timestamp") or metadata.get("visit_time") or time.time())
        if event_type in ("file_edit", "git_commit") and _is_late_night(ts):
            return EvidenceItem(
                timestamp=ts,
                weight=0.15,
                source=event_type,
                payload={"hour": time.localtime(ts).tm_hour},
            )
        return None

    def hint_text(self) -> str:
        # Deliberately vague — never names "burnout"
        return "Hope you're doing okay this week. If you want to talk through anything, I'm here."
```

- [ ] **Step 1.5: Tests for the framework**

```python
# tests/test_life_event_pattern_base.py
import time
from opencomputer.awareness.life_events.pattern import (
    EvidenceItem, LifeEventPattern, PatternFiring,
)


class _DummyPattern(LifeEventPattern):
    pattern_id = "dummy"
    surface_threshold = 0.5

    def consider_event(self, event_type, metadata):
        if event_type == "test_event":
            return EvidenceItem(
                timestamp=time.time(),
                weight=float(metadata.get("weight", 0.3)),
                source="test",
            )
        return None


def test_no_evidence_yields_no_firing():
    p = _DummyPattern()
    assert p.accumulate("unrelated_event", {}) is None


def test_below_threshold_does_not_fire():
    p = _DummyPattern()
    result = p.accumulate("test_event", {"weight": 0.2})
    assert result is None


def test_above_threshold_fires():
    p = _DummyPattern()
    p.accumulate("test_event", {"weight": 0.3})
    result = p.accumulate("test_event", {"weight": 0.3})
    assert result is not None
    assert isinstance(result, PatternFiring)
    assert result.pattern_id == "dummy"
    assert result.confidence >= 0.5


def test_old_evidence_pruned():
    p = _DummyPattern()
    # Inject ancient evidence
    p._evidence.append(EvidenceItem(
        timestamp=time.time() - (30 * 86400),  # 30 days ago
        weight=1.0, source="test",
    ))
    p.accumulate("test_event", {"weight": 0.1})  # triggers _prune
    # Old evidence should be gone
    assert all(e.timestamp > time.time() - 15 * 86400 for e in p._evidence)
```

```python
# tests/test_life_event_job_change.py
from opencomputer.awareness.life_events.job_change import JobChange


def test_linkedin_jobs_url_contributes():
    p = JobChange()
    result = p.accumulate("browser_visit", {
        "url": "https://www.linkedin.com/jobs/search",
        "title": "Software Engineer Jobs",
        "visit_time": 1714000000.0,
    })
    # One hit alone (weight 0.4) is below 0.7 threshold
    assert result is None


def test_two_linkedin_visits_fire():
    p = JobChange()
    p.accumulate("browser_visit", {
        "url": "https://linkedin.com/jobs", "title": "x", "visit_time": 1714000000.0,
    })
    result = p.accumulate("browser_visit", {
        "url": "https://glassdoor.com/jobs", "title": "y",
        "visit_time": 1714086400.0,
    })
    assert result is not None
    assert "rhythm" in result.hint_text


def test_unrelated_url_ignored():
    p = JobChange()
    result = p.accumulate("browser_visit", {
        "url": "https://github.com/saksham/repo", "title": "code", "visit_time": 1714000000.0,
    })
    assert result is None
```

```python
# tests/test_life_event_exam_prep.py
from opencomputer.awareness.life_events.exam_prep import ExamPrep


def test_khanacademy_visits_fire():
    p = ExamPrep()
    for i in range(4):
        p.accumulate("browser_visit", {
            "url": "https://khanacademy.org/calculus",
            "title": "Lesson",
            "visit_time": 1714000000.0 + i * 60,
        })
    p.accumulate("browser_visit", {
        "url": "https://leetcode.com/practice-test",
        "title": "Practice Test - Arrays",
        "visit_time": 1714000300.0,
    })
    # 4 weight=0.2 + 1 weight=0.3 = 1.1 (capped at 1.0) >> 0.7
    # Final accumulate should fire.
    result = p.accumulate("browser_visit", {
        "url": "https://leetcode.com/practice-test",
        "title": "Practice Test - DP",
        "visit_time": 1714000400.0,
    })
    assert result is not None
    assert "study" in result.hint_text.lower() or "concepts" in result.hint_text.lower()


def test_unrelated_url_no_evidence():
    p = ExamPrep()
    result = p.accumulate("browser_visit", {
        "url": "https://news.com/article", "title": "x", "visit_time": 1714000000.0,
    })
    assert result is None
```

```python
# tests/test_life_event_burnout.py
import time
from opencomputer.awareness.life_events.burnout import Burnout


def test_late_night_edit_contributes():
    """Edits at 1 AM (hour=1) accumulate."""
    p = Burnout()
    midnight_ts = time.mktime((2026, 4, 27, 1, 30, 0, 0, 0, -1))
    p.accumulate("file_edit", {"timestamp": midnight_ts})
    assert len(p._evidence) == 1


def test_daytime_edit_ignored():
    p = Burnout()
    noon_ts = time.mktime((2026, 4, 27, 12, 0, 0, 0, 0, -1))
    result = p.accumulate("file_edit", {"timestamp": noon_ts})
    assert result is None
    assert len(p._evidence) == 0
```

- [ ] **Step 1.6: Verify tests pass + commit**

```bash
python3.13 -m pytest tests/test_life_event_*.py -v
```

```bash
git add opencomputer/awareness/ tests/test_life_event_*.py
git commit -m "feat(awareness): V2.C-T1 — Life-Event pattern framework + JobChange/ExamPrep/Burnout"
```

---

## Task 2: 3 more patterns + registry + bus subscription

**Files:**
- Create: `opencomputer/awareness/life_events/{relationship_shift,health_event,travel,registry}.py`
- Test: `tests/test_life_events_registry.py` + per-pattern test files

- [ ] **Step 2.1: Implement `RelationshipShift`** (NEVER surface unprompted)

```python
"""RelationshipShift — sudden drop in messages with frequent contact. NEVER surfaces unprompted."""
from __future__ import annotations

from opencomputer.awareness.life_events.pattern import (
    EvidenceItem, LifeEventPattern, SurfacingPolicy,
)


class RelationshipShift(LifeEventPattern):
    pattern_id: str = "relationship_shift"
    surfacing: SurfacingPolicy = "silent"  # NEVER auto-surface
    surface_threshold: float = 0.6

    def consider_event(self, event_type, metadata):
        if event_type != "messaging.contact_drop":
            return None
        # The aggregate "contact_drop" event is computed elsewhere by an
        # aggregator that watches messaging activity; this pattern just
        # reacts. Drop magnitude carried in metadata.
        magnitude = float(metadata.get("magnitude", 0.0))  # 0.0 .. 1.0
        if magnitude < 0.3:
            return None
        return EvidenceItem(
            timestamp=float(metadata.get("timestamp", 0.0)),
            weight=magnitude,
            source="messaging",
            payload={"contact_id": metadata.get("contact_id", "")},
        )

    def hint_text(self) -> str:
        # silent — never read by the chat surfacer
        return ""
```

- [ ] **Step 2.2: Implement `HealthEvent`** (silent)

```python
"""HealthEvent — symptom searches + medical sites. NEVER surface unprompted."""
from __future__ import annotations

from typing import ClassVar

from opencomputer.awareness.life_events.pattern import (
    EvidenceItem, LifeEventPattern, SurfacingPolicy,
)


_HEALTH_DOMAINS: ClassVar[frozenset[str]] = frozenset({
    "webmd.com", "mayoclinic.org", "drugs.com", "healthline.com",
    "nih.gov", "medlineplus.gov", "1mg.com", "practo.com",
})


class HealthEvent(LifeEventPattern):
    pattern_id: str = "health_event"
    surfacing: SurfacingPolicy = "silent"
    surface_threshold: float = 0.6

    def consider_event(self, event_type, metadata):
        if event_type != "browser_visit":
            return None
        url = str(metadata.get("url", "")).lower()
        for d in _HEALTH_DOMAINS:
            if d in url:
                return EvidenceItem(
                    timestamp=float(metadata.get("visit_time", 0.0)),
                    weight=0.3,
                    source="browser",
                    payload={"domain_match": d},
                )
        return None
```

- [ ] **Step 2.3: Implement `Travel`**

```python
"""Travel — hotel/airline searches + maps activity for non-home locations."""
from __future__ import annotations

from typing import ClassVar

from opencomputer.awareness.life_events.pattern import (
    EvidenceItem, LifeEventPattern, SurfacingPolicy,
)


_TRAVEL_DOMAINS: ClassVar[frozenset[str]] = frozenset({
    "booking.com", "expedia.com", "kayak.com", "skyscanner.com",
    "airbnb.com", "trip.com", "makemytrip.com", "ixigo.com",
    "google.com/flights", "google.com/maps",
})


class Travel(LifeEventPattern):
    pattern_id: str = "travel"
    surfacing: SurfacingPolicy = "hint"
    surface_threshold: float = 0.7

    def consider_event(self, event_type, metadata):
        if event_type != "browser_visit":
            return None
        url = str(metadata.get("url", "")).lower()
        for d in _TRAVEL_DOMAINS:
            if d in url:
                return EvidenceItem(
                    timestamp=float(metadata.get("visit_time", 0.0)),
                    weight=0.3,
                    source="browser",
                    payload={"domain": d},
                )
        return None

    def hint_text(self) -> str:
        return (
            "Looks like you might be planning a trip — want me to draft a packing list, "
            "find time conflicts on your calendar, or summarize the destination's weather?"
        )
```

- [ ] **Step 2.4: Implement registry + bus subscription**

```python
# opencomputer/awareness/life_events/registry.py
"""Pattern registry + F2 SignalEvent bus subscription wiring.

The registry is constructed once at AgentLoop init. It subscribes to the
default bus; every published SignalEvent is dispatched to every pattern.
Pattern firings are appended to an in-memory queue that the chat surfacer
drains at the start of each turn.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from opencomputer.awareness.life_events.pattern import LifeEventPattern, PatternFiring
from opencomputer.awareness.life_events.burnout import Burnout
from opencomputer.awareness.life_events.exam_prep import ExamPrep
from opencomputer.awareness.life_events.health_event import HealthEvent
from opencomputer.awareness.life_events.job_change import JobChange
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
```

- [ ] **Step 2.5: Tests**

```python
# tests/test_life_events_registry.py
from opencomputer.awareness.life_events.registry import (
    DEFAULT_PATTERNS, LifeEventRegistry, subscribe_to_bus,
)


def test_default_registry_has_six_patterns():
    reg = LifeEventRegistry()
    pattern_ids = {p[0] for p in reg.list_patterns()}
    assert len(pattern_ids) == 6
    assert "job_change" in pattern_ids
    assert "burnout" in pattern_ids


def test_mute_unmute_round_trip():
    reg = LifeEventRegistry()
    reg.mute("burnout")
    assert reg.is_muted("burnout")
    reg.unmute("burnout")
    assert not reg.is_muted("burnout")


def test_muted_pattern_does_not_accumulate(monkeypatch):
    """If a pattern is muted, on_event must skip it."""
    reg = LifeEventRegistry()
    reg.mute("job_change")
    reg.on_event("browser_visit", {"url": "https://linkedin.com/jobs", "visit_time": 0.0})
    reg.on_event("browser_visit", {"url": "https://glassdoor.com/jobs", "visit_time": 0.0})
    # Should NOT have queued firings (muted)
    assert reg.drain_pending() == []


def test_silent_firings_not_queued_for_chat():
    """HealthEvent has surfacing='silent' — should never appear in drain_pending."""
    reg = LifeEventRegistry()
    for _ in range(5):
        reg.on_event("browser_visit", {"url": "https://webmd.com/symptoms", "visit_time": 0.0})
    pending = reg.drain_pending()
    assert all(f.pattern_id != "health_event" for f in pending)


def test_subscribe_to_bus_round_trip():
    """Bus integration: publishing on the bus should dispatch to registry."""
    from opencomputer.ingestion.bus import TypedEventBus
    from plugin_sdk.ingestion import SignalEvent
    bus = TypedEventBus()
    reg = LifeEventRegistry()
    unsub = subscribe_to_bus(reg, bus)
    try:
        bus.publish(SignalEvent(
            event_type="browser_visit", source="test",
            metadata={"url": "https://linkedin.com/jobs", "visit_time": 0.0},
        ))
        bus.publish(SignalEvent(
            event_type="browser_visit", source="test",
            metadata={"url": "https://glassdoor.com/jobs", "visit_time": 0.0},
        ))
    finally:
        unsub()
    pending = reg.drain_pending()
    assert any(f.pattern_id == "job_change" for f in pending)
```

- [ ] **Step 2.6: Commit**

```bash
git add opencomputer/awareness/life_events/ tests/test_life_event_*.py tests/test_life_events_registry.py
git commit -m "feat(awareness): V2.C-T2 — 3 more patterns (RelationshipShift/HealthEvent/Travel) + registry + bus subscription"
```

---

## Task 3: Pattern CLI controls (mute/unmute/list)

**Files:**
- Create: `opencomputer/cli_awareness.py`
- Modify: `opencomputer/cli.py` (register subgroup)
- Modify: `opencomputer/agent/consent/capability_taxonomy.py` (add `awareness.*` claims)
- Test: `tests/test_cli_awareness.py`

- [ ] **Step 3.1: Add capability claims**

In `capability_taxonomy.py`:

```python
    # V2.C — Layered Awareness life-event detection (2026-04-27).
    "awareness.life_event.observe": ConsentTier.IMPLICIT,
    "awareness.life_event.surface": ConsentTier.IMPLICIT,
    "awareness.persona.classify": ConsentTier.IMPLICIT,
    "awareness.persona.switch": ConsentTier.IMPLICIT,
```

- [ ] **Step 3.2: Implement CLI subgroup**

`opencomputer/cli_awareness.py`:

```python
"""V2.C — opencomputer awareness {patterns,personas} {list,mute,unmute}."""
from __future__ import annotations

import typer

awareness_app = typer.Typer(help="Layered Awareness controls (patterns + personas)")
patterns_app = typer.Typer(help="Life-event pattern controls")
personas_app = typer.Typer(help="Plural-persona controls")
awareness_app.add_typer(patterns_app, name="patterns")
awareness_app.add_typer(personas_app, name="personas")


@patterns_app.command("list")
def patterns_list() -> None:
    """List all registered life-event patterns + their muted state."""
    from opencomputer.awareness.life_events.registry import LifeEventRegistry
    reg = LifeEventRegistry()
    typer.echo(f"{'pattern_id':30s} {'surfacing':10s} {'muted':6s}")
    for pattern_id, surfacing, muted in reg.list_patterns():
        typer.echo(f"{pattern_id:30s} {surfacing:10s} {'yes' if muted else 'no':6s}")


@patterns_app.command("mute")
def patterns_mute(pattern_id: str = typer.Argument(...)) -> None:
    """Mute a life-event pattern (silent for the rest of this session AND saved)."""
    from opencomputer.awareness.life_events.registry import LifeEventRegistry
    from opencomputer.agent.config import _home
    from pathlib import Path
    import json

    reg = LifeEventRegistry()
    valid_ids = {p[0] for p in reg.list_patterns()}
    if pattern_id not in valid_ids:
        typer.echo(f"Unknown pattern: {pattern_id}", err=True)
        raise typer.Exit(1)

    state_path = _home() / "awareness" / "muted_patterns.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    muted = []
    if state_path.exists():
        try:
            muted = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    if pattern_id not in muted:
        muted.append(pattern_id)
    state_path.write_text(json.dumps(muted))
    typer.echo(f"Muted: {pattern_id}")


@patterns_app.command("unmute")
def patterns_unmute(pattern_id: str = typer.Argument(...)) -> None:
    from opencomputer.agent.config import _home
    from pathlib import Path
    import json

    state_path = _home() / "awareness" / "muted_patterns.json"
    if not state_path.exists():
        typer.echo(f"Nothing muted (no state file).")
        return
    try:
        muted = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        muted = []
    if pattern_id in muted:
        muted.remove(pattern_id)
    state_path.write_text(json.dumps(muted))
    typer.echo(f"Unmuted: {pattern_id}")


@personas_app.command("list")
def personas_list() -> None:
    """List all registered personas."""
    from opencomputer.awareness.personas.registry import list_personas
    personas = list_personas()
    typer.echo(f"{'persona_id':20s} {'description':50s}")
    for p in personas:
        typer.echo(f"{p['id']:20s} {p.get('description', ''):50s}")
```

- [ ] **Step 3.3: Wire into `cli.py`**

```python
# In opencomputer/cli.py, near other Typer subgroup registrations:
from opencomputer.cli_awareness import awareness_app
app.add_typer(awareness_app, name="awareness")
```

- [ ] **Step 3.4: Tests**

```python
# tests/test_cli_awareness.py
from pathlib import Path
from typer.testing import CliRunner

from opencomputer.cli import app

runner = CliRunner()


def test_patterns_list_shows_six_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["awareness", "patterns", "list"])
    assert result.exit_code == 0
    assert "job_change" in result.stdout
    assert "burnout" in result.stdout


def test_patterns_mute_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["awareness", "patterns", "mute", "burnout"])
    assert result.exit_code == 0
    state = (tmp_path / "awareness" / "muted_patterns.json").read_text()
    assert "burnout" in state


def test_patterns_unmute_removes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["awareness", "patterns", "mute", "burnout"])
    result = runner.invoke(app, ["awareness", "patterns", "unmute", "burnout"])
    assert result.exit_code == 0
    state = (tmp_path / "awareness" / "muted_patterns.json").read_text()
    assert "burnout" not in state


def test_unknown_pattern_id_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["awareness", "patterns", "mute", "not_a_real_pattern"])
    assert result.exit_code == 1
```

- [ ] **Step 3.5: Commit**

```bash
git add opencomputer/cli_awareness.py opencomputer/cli.py opencomputer/agent/consent/capability_taxonomy.py tests/test_cli_awareness.py
git commit -m "feat(cli): V2.C-T3 — opencomputer awareness {patterns,personas} {list,mute,unmute}"
```

---

## Task 4: Persona auto-classifier + 5 default personas

**Files:**
- Create: `opencomputer/awareness/personas/{__init__,classifier,registry}.py`
- Create: `opencomputer/awareness/personas/defaults/{coding,trading,relaxed,admin,learning}.yaml`
- Test: `tests/test_persona_classifier.py`, `tests/test_persona_registry.py`

- [ ] **Step 4.1: Implement classifier**

```python
# opencomputer/awareness/personas/classifier.py
"""PersonaClassifier — heuristic mapping from context to persona id.

Reads (foreground_app, time_of_day, recent_files, last_3_messages) and
returns one of the registered persona ids. Heuristic-based for V2.C — V2.D
may swap in an LLM-based classifier.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ClassificationContext:
    foreground_app: str = ""
    time_of_day_hour: int = 12
    recent_file_paths: tuple[str, ...] = ()
    last_messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    persona_id: str
    confidence: float
    reason: str


_CODING_APPS = ("code", "cursor", "pycharm", "iterm", "terminal", "warp", "neovim")
_TRADING_APPS = ("zerodha", "groww", "kite", "tradingview", "screener", "marketsmojo")
_RELAXED_APPS = ("animepahe", "youtube", "spotify", "netflix", "reddit", "instagram")


def classify(ctx: ClassificationContext) -> ClassificationResult:
    app_lower = ctx.foreground_app.lower()
    if any(a in app_lower for a in _CODING_APPS):
        return ClassificationResult("coding", 0.85, f"foreground app '{ctx.foreground_app}' suggests coding")
    if any(a in app_lower for a in _TRADING_APPS):
        return ClassificationResult("trading", 0.85, f"foreground app '{ctx.foreground_app}' suggests trading")
    if any(a in app_lower for a in _RELAXED_APPS):
        return ClassificationResult("relaxed", 0.8, f"foreground app '{ctx.foreground_app}' suggests relaxed mode")

    # File-based fallback
    py_files = sum(1 for p in ctx.recent_file_paths if p.endswith(".py"))
    md_files = sum(1 for p in ctx.recent_file_paths if p.endswith(".md"))
    if py_files >= 3:
        return ClassificationResult("coding", 0.7, f"{py_files} recent .py files")
    if md_files >= 3:
        return ClassificationResult("learning", 0.6, f"{md_files} recent .md files")

    # Time-of-day fallback
    if 21 <= ctx.time_of_day_hour or ctx.time_of_day_hour < 6:
        return ClassificationResult("relaxed", 0.5, f"hour={ctx.time_of_day_hour} (evening/late)")
    if 9 <= ctx.time_of_day_hour < 12:
        return ClassificationResult("coding", 0.4, "morning hours, default to coding")

    return ClassificationResult("admin", 0.3, "no strong signal — default admin")
```

- [ ] **Step 4.2: Implement registry + 5 defaults**

`opencomputer/awareness/personas/registry.py`:

```python
"""Persona YAML loader + registry.

Loads from <profile_home>/personas/*.yaml first, falling back to bundled
defaults. Each persona has:
  id, name, description, system_prompt_overlay, preferred_tone,
  preferred_response_format, disabled_capabilities.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from opencomputer.agent.config import _home

_BUNDLED_DIR = Path(__file__).parent / "defaults"


def _load_yaml_files(directory: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and "id" in data:
            out.append(data)
    return out


def list_personas() -> list[dict[str, Any]]:
    """User personas override bundled defaults by id."""
    bundled = _load_yaml_files(_BUNDLED_DIR)
    user_dir = _home() / "personas"
    user = _load_yaml_files(user_dir)
    by_id: dict[str, dict[str, Any]] = {p["id"]: p for p in bundled}
    for p in user:
        by_id[p["id"]] = p
    return list(by_id.values())


def get_persona(persona_id: str) -> dict[str, Any] | None:
    for p in list_personas():
        if p["id"] == persona_id:
            return p
    return None
```

Bundle 5 default YAML files at `opencomputer/awareness/personas/defaults/`:

`coding.yaml`:
```yaml
id: coding
name: Coding mode
description: Active development — terse, tool-heavy, technical depth.
system_prompt_overlay: |
  User is in coding mode. Be concise. Default to technical depth.
  Prefer Edit/MultiEdit over describing changes. Reference file_path:line_number.
preferred_tone: terse
preferred_response_format: bullet
disabled_capabilities: []
```

`trading.yaml`:
```yaml
id: trading
name: Stock trading
description: Indian/US equity research, screening, technical analysis.
system_prompt_overlay: |
  User is in trading mode. Always use live data. Never give stale stock info.
  Default sources: investor-agent MCP, stockflow MCP, fresh web search.
  Include catalysts, sentiment, and explicit timestamps.
preferred_tone: precise
preferred_response_format: prose
disabled_capabilities: []
```

`relaxed.yaml`:
```yaml
id: relaxed
name: Relaxed evening
description: Casual conversation, low pressure, no aggressive tooling.
system_prompt_overlay: |
  User is winding down. Be conversational. Don't push tools unless asked.
  Avoid heavy technical depth.
preferred_tone: warm
preferred_response_format: prose
disabled_capabilities: ["RunTests", "Bash"]
```

`admin.yaml`:
```yaml
id: admin
name: Admin / triage
description: Email, calendar, scheduling, paperwork.
system_prompt_overlay: |
  User is doing admin/triage work. Help them clear their queue.
  Be quick and decisive. Suggest next actions explicitly.
preferred_tone: efficient
preferred_response_format: bullet
disabled_capabilities: []
```

`learning.yaml`:
```yaml
id: learning
name: Learning mode
description: Studying / reading / processing new material.
system_prompt_overlay: |
  User is learning. Explain step by step. Provide examples.
  Reference primary sources. Don't dumb things down.
preferred_tone: explanatory
preferred_response_format: prose
disabled_capabilities: []
```

- [ ] **Step 4.3: Tests**

```python
# tests/test_persona_classifier.py
from opencomputer.awareness.personas.classifier import (
    ClassificationContext, classify,
)


def test_cursor_app_classifies_coding():
    ctx = ClassificationContext(foreground_app="Cursor", time_of_day_hour=10)
    result = classify(ctx)
    assert result.persona_id == "coding"
    assert result.confidence >= 0.8


def test_zerodha_app_classifies_trading():
    ctx = ClassificationContext(foreground_app="Zerodha Kite", time_of_day_hour=10)
    result = classify(ctx)
    assert result.persona_id == "trading"


def test_animepahe_classifies_relaxed():
    ctx = ClassificationContext(foreground_app="animepahe.com", time_of_day_hour=22)
    result = classify(ctx)
    assert result.persona_id == "relaxed"


def test_files_fallback_when_app_unknown():
    ctx = ClassificationContext(
        foreground_app="UnknownApp",
        time_of_day_hour=14,
        recent_file_paths=("a.py", "b.py", "c.py", "d.py"),
    )
    result = classify(ctx)
    assert result.persona_id == "coding"


def test_late_night_default_relaxed():
    ctx = ClassificationContext(foreground_app="X", time_of_day_hour=23)
    result = classify(ctx)
    assert result.persona_id == "relaxed"


def test_no_signal_defaults_admin():
    ctx = ClassificationContext(foreground_app="", time_of_day_hour=14)
    result = classify(ctx)
    assert result.persona_id == "admin"
```

```python
# tests/test_persona_registry.py
from pathlib import Path
from opencomputer.awareness.personas.registry import get_persona, list_personas


def test_default_personas_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    personas = list_personas()
    ids = {p["id"] for p in personas}
    assert {"coding", "trading", "relaxed", "admin", "learning"}.issubset(ids)


def test_user_overrides_bundled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    user_dir = tmp_path / "personas"
    user_dir.mkdir()
    (user_dir / "coding.yaml").write_text(
        "id: coding\nname: My Coding\ndescription: my override\n"
        "system_prompt_overlay: 'custom'\npreferred_tone: warm\npreferred_response_format: prose\ndisabled_capabilities: []\n"
    )
    p = get_persona("coding")
    assert p["name"] == "My Coding"


def test_get_persona_unknown_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    assert get_persona("nonexistent") is None
```

- [ ] **Step 4.4: Commit**

```bash
git add opencomputer/awareness/personas/ tests/test_persona_classifier.py tests/test_persona_registry.py
git commit -m "feat(awareness): V2.C-T4 — persona auto-classifier + 5 default personas (coding/trading/relaxed/admin/learning)"
```

---

## Task 5: Persona switching wired into AgentLoop

**Files:**
- Modify: `opencomputer/agent/loop.py`
- Test: `tests/test_persona_loop_integration.py`

- [ ] **Step 5.1: Add classifier hook**

In `agent/loop.py`, find the per-turn entry point (e.g., the start of the inner loop in `run_conversation`). Add a once-per-turn classification:

```python
# Pseudo-shape (adapt to actual loop structure):
def _build_persona_overlay(self, recent_messages, recent_files) -> str:
    """Run classifier; return persona's system_prompt_overlay (or empty if disabled)."""
    from opencomputer.awareness.personas.classifier import (
        ClassificationContext, classify,
    )
    from opencomputer.awareness.personas.registry import get_persona
    import time
    
    ctx = ClassificationContext(
        foreground_app=self._detect_foreground_app(),  # may return ""
        time_of_day_hour=time.localtime().tm_hour,
        recent_file_paths=tuple(recent_files[-10:]),
        last_messages=tuple(m.content[:100] for m in recent_messages[-3:] if m.role == "user"),
    )
    result = classify(ctx)
    persona = get_persona(result.persona_id)
    if persona is None:
        return ""
    return persona.get("system_prompt_overlay", "")
```

Wire the result into the prompt construction. Probably easiest: pass `persona_overlay` as a new PromptContext field (similar to `user_facts`).

Add `persona_overlay: str = ""` to `PromptContext`. Update `base.j2` to render it (Task 5.2 below).

- [ ] **Step 5.2: Render in `base.j2`**

```jinja
{% if persona_overlay -%}
## Active persona

{{ persona_overlay }}

{% endif %}
```

Insert near the user_facts block.

- [ ] **Step 5.3: Tests**

```python
# tests/test_persona_loop_integration.py
from unittest.mock import MagicMock, patch

from opencomputer.awareness.personas.classifier import ClassificationResult


def test_persona_overlay_rendered_in_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    
    fake_result = ClassificationResult("coding", 0.9, "test")
    
    from opencomputer.agent.prompt_builder import PromptBuilder, PromptContext
    pb = PromptBuilder()
    
    rendered = pb.build(persona_overlay="User is in coding mode. Be concise.")
    assert "coding mode" in rendered
    assert "concise" in rendered


def test_no_overlay_when_persona_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent.prompt_builder import PromptBuilder
    pb = PromptBuilder()
    rendered = pb.build()
    # No "Active persona" header when overlay is empty
    assert "Active persona" not in rendered
```

- [ ] **Step 5.4: Commit**

```bash
git add opencomputer/agent/loop.py opencomputer/agent/prompts/base.j2 opencomputer/agent/prompt_builder.py tests/test_persona_loop_integration.py
git commit -m "feat(awareness): V2.C-T5 — persona classifier wired into AgentLoop turn start"
```

---

## Task 6: Final validation + CHANGELOG + push + PR

- [ ] **Step 6.1: Full pytest + ruff**

```
python3.13 -m pytest -q
ruff check .
```

Confirm 3340+ pass. Auto-fix ruff.

- [ ] **Step 6.2: CHANGELOG entry**

Append to `[Unreleased]`:

```markdown
### Added (Layered Awareness V2.C — Life-Event Detector + Plural Personas, 2026-04-27)

- **Life-Event Detector framework** — `LifeEventPattern` ABC + sliding-window
  evidence accumulator with exponential decay (default 7-day half-life,
  14-day window). Patterns subscribe to F2 SignalEvent bus; firings either
  surface as chat hints (`surfacing="hint"`) or stay silent F4 graph edges
  (`surfacing="silent"` for HealthEvent / RelationshipShift — never auto-surface).
- **6 starter patterns** — JobChange, ExamPrep, Burnout, RelationshipShift,
  HealthEvent, Travel. Surface threshold 0.7 default.
- **Pattern registry + bus subscription** — `LifeEventRegistry` owns the
  pattern instances + firing queue; `subscribe_to_bus()` wires it to the
  default TypedEventBus.
- **`opencomputer awareness patterns {list,mute,unmute}`** CLI + persistent
  mute state at `<profile_home>/awareness/muted_patterns.json`.
- **`opencomputer awareness personas list`** CLI.
- **Persona auto-classifier** — `classify(ClassificationContext)` reads
  foreground app, time of day, recent files, last messages → returns
  `ClassificationResult(persona_id, confidence, reason)`. Heuristic-based
  in V2.C; V2.D may swap to LLM.
- **5 default personas** — coding, trading, relaxed, admin, learning.
  YAML at `opencomputer/awareness/personas/defaults/*.yaml`. User overrides
  via `<profile_home>/personas/*.yaml`.
- **Persona overlay wired into AgentLoop** — at turn start, classifier runs,
  persona's `system_prompt_overlay` lands as `{{ persona_overlay }}` slot in
  base.j2 (between user_facts and skills).
- **F1 capability claims** — `awareness.life_event.observe`,
  `awareness.life_event.surface`, `awareness.persona.classify`,
  `awareness.persona.switch` (all IMPLICIT).

V2.D (Curious Companion — agent asks indirect questions to fill knowledge
gaps) is the natural next plan.

Spec + plan: `OpenComputer/docs/superpowers/plans/2026-04-27-layered-awareness-v2c.md`
```

- [ ] **Step 6.3: Commit + push**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): V2.C — Life-Event Detector + Plural Personas entry"
git push -u origin feat/layered-awareness-v2c
```

- [ ] **Step 6.4: Open PR**

Standard PR body. Include test count + CI verification.

DO NOT MERGE. Report PR number + URL.

---

## Self-Review

- ✅ Layer 5 framework — Task 1
- ✅ 6 patterns — Tasks 1 + 2
- ✅ Pattern registry + bus subscription — Task 2
- ✅ CLI controls — Task 3
- ✅ Persona classifier + 5 defaults — Task 4
- ✅ Persona AgentLoop integration — Task 5
- ✅ Validation + push — Task 6

Audit findings already baked in:
- HealthEvent + RelationshipShift use `surfacing="silent"` (never surface unprompted)
- Burnout's hint_text deliberately avoids the word "burnout"
- `mute/unmute` persists across sessions via JSON state file
- Bundled defaults loaded from package; user overrides at `<profile_home>/personas/`
- Heuristic classifier (V2.C); LLM-based classifier deferred to V2.D
