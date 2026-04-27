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
