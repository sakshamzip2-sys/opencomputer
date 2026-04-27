"""HealthEvent — symptom searches + medical sites. NEVER surface unprompted."""
from __future__ import annotations

from typing import ClassVar

from opencomputer.awareness.life_events.pattern import (
    EvidenceItem,
    LifeEventPattern,
    SurfacingPolicy,
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
