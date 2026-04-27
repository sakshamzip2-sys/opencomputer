"""JobChange — sudden drop in work-mail volume + LinkedIn searches + resignation/severance terms."""
from __future__ import annotations

from typing import ClassVar

from opencomputer.awareness.life_events.pattern import (
    EvidenceItem,
    LifeEventPattern,
    SurfacingPolicy,
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
