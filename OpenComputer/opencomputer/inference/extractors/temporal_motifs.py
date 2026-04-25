"""
Temporal motif extractor — recurring usage by (hour-of-day, weekday).

Buckets events into a 24x7 grid. For each bucket with >= 3 events
sharing ``event_type``, ``source``, and ``tool_name`` (when present),
emits one :class:`plugin_sdk.inference.Motif` describing the recurring
pattern.

Privacy posture
---------------

Only labels, hours, weekdays and counts are stored — never raw user
content. A consumer who needs the underlying messages retrieves them
from SessionDB by ``event_id``.
"""

from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from collections.abc import Sequence
from typing import ClassVar

from plugin_sdk.inference import Motif, MotifKind
from plugin_sdk.ingestion import SignalEvent, ToolCallEvent

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# Minimum events per (hour, weekday, label) bucket before we emit a motif.
# Below this threshold the signal is too noisy to be useful.
_MIN_BUCKET_SUPPORT = 3


def _label_for(event: SignalEvent) -> str:
    """Pick the best label string for grouping.

    Tool-call events use ``tool_name``; everything else falls back to
    ``source``. Empty strings mean "no usable label" — those buckets
    get filtered out before we emit motifs (an unlabelled "the user
    did something" motif has no actionable meaning).
    """
    if isinstance(event, ToolCallEvent) and event.tool_name:
        return event.tool_name
    return event.source


class TemporalMotifExtractor:
    """Bucket-by-(hour, weekday) recurring usage detector.

    Implements :class:`plugin_sdk.inference.MotifExtractor`. Pure —
    does not mutate the bus, the store, or any module-level state.
    """

    name: ClassVar[str] = "temporal"
    kind: ClassVar[MotifKind] = "temporal"

    def extract(self, events: Sequence[SignalEvent]) -> list[Motif]:
        """Emit one motif per (hour, weekday, label) bucket with >= 3 events.

        Bucket key = ``(hour, day_of_week, event_type, source, label)``.
        Different tools or sources stay separate, so two motifs at the
        same hour but different labels are independently observable.

        Confidence is ``min(1.0, count / 10)``: 10 hits per bucket
        saturates at 1.0, which is a reasonable "this is clearly
        habitual" cutoff.
        """
        # Bucket key: (hour, day_of_week, event_type, source, label).
        # Including event_type + source in the key means two
        # independent kinds of activity that happen to land in the
        # same hour stay separate.
        buckets: dict[tuple[int, int, str, str, str], list[SignalEvent]] = (
            defaultdict(list)
        )
        for event in events:
            label = _label_for(event)
            if not label:
                continue
            ts = _dt.datetime.fromtimestamp(event.timestamp, tz=_dt.UTC)
            key = (
                ts.hour,
                ts.weekday(),
                event.event_type,
                event.source,
                label,
            )
            buckets[key].append(event)

        motifs: list[Motif] = []
        for (hour, dow, _etype, _src, label), bucket_events in buckets.items():
            count = len(bucket_events)
            if count < _MIN_BUCKET_SUPPORT:
                continue
            confidence = min(1.0, count / 10)
            weekday_name = _WEEKDAY_NAMES[dow]
            summary = (
                f"User runs {label} most often on {weekday_name} "
                f"between {hour:02}:00–{hour:02}:59 "
                f"(n={count}, conf={confidence:.2f})"
            )
            payload = {
                "hour": hour,
                "day_of_week": dow,
                "label": label,
                "count": count,
            }
            evidence = tuple(e.event_id for e in bucket_events)
            motifs.append(
                Motif(
                    kind="temporal",
                    confidence=confidence,
                    support=count,
                    summary=summary,
                    payload=payload,
                    evidence_event_ids=evidence,
                )
            )
        return motifs


__all__ = ["TemporalMotifExtractor"]
