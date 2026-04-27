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
