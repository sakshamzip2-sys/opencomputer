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
