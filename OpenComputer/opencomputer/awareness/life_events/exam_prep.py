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
