"""
Transition-chain extractor — adjacent-event follow-on counts.

Sorts events by timestamp; for each adjacent pair within a 5-minute
window, records the transition ``f"{event_type}/{tool_name or source}"
→ f"{event_type}/{tool_name or source}"`` in a Counter. Transitions
seen >= 2 times are emitted as :class:`plugin_sdk.inference.Motif`
records.

Privacy posture
---------------

Same as the temporal extractor: labels, counts, probabilities and
event ids only. No raw user content.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import ClassVar

from plugin_sdk.inference import Motif, MotifKind
from plugin_sdk.ingestion import SignalEvent, ToolCallEvent

#: Maximum gap (seconds) between adjacent events before they no longer
#: count as a transition. 5 minutes lines up with how a human user
#: typically sequences related activity; longer gaps usually mean
#: "different intent".
_TRANSITION_WINDOW_SECONDS = 300

#: Minimum count before a transition is emitted as a motif.
_MIN_TRANSITION_COUNT = 2


def _label_for(event: SignalEvent) -> str:
    """Format ``event_type/tool_name_or_source`` for transition keys.

    Empty source falls back to a literal ``"unknown"`` so the produced
    string never collapses to a bare slash. Tool-call events prefer
    ``tool_name`` over ``source``.
    """
    if isinstance(event, ToolCallEvent) and event.tool_name:
        suffix = event.tool_name
    elif event.source:
        suffix = event.source
    else:
        suffix = "unknown"
    return f"{event.event_type}/{suffix}"


class TransitionChainExtractor:
    """5-minute-window adjacent-event transition counter.

    Implements :class:`plugin_sdk.inference.MotifExtractor`. Pure.
    """

    name: ClassVar[str] = "transition"
    kind: ClassVar[MotifKind] = "transition"

    def extract(self, events: Sequence[SignalEvent]) -> list[Motif]:
        """Emit one motif per (prev → curr) transition seen >= 2 times.

        The probability ``count / total_after_prev`` represents
        "given the user just did ``prev``, how often did they next do
        ``curr``?" — a simple Markov-1 estimate. Confidence is
        ``min(1.0, prob * (count / 5))`` which favours frequent
        high-probability transitions over rare ones.
        """
        if len(events) < 2:
            return []

        sorted_events = sorted(events, key=lambda e: e.timestamp)
        # Counter keyed on the transition string pair; we keep both the
        # raw counter and a "events that contributed" mapping so we can
        # populate evidence_event_ids on the resulting motifs.
        transitions: Counter[tuple[str, str]] = Counter()
        evidence_by_pair: dict[tuple[str, str], list[str]] = {}
        from_prev_total: Counter[str] = Counter()

        for prev, curr in zip(sorted_events, sorted_events[1:], strict=False):
            gap = curr.timestamp - prev.timestamp
            if gap > _TRANSITION_WINDOW_SECONDS:
                continue
            prev_label = _label_for(prev)
            curr_label = _label_for(curr)
            key = (prev_label, curr_label)
            transitions[key] += 1
            from_prev_total[prev_label] += 1
            evidence_by_pair.setdefault(key, []).extend(
                (prev.event_id, curr.event_id)
            )

        motifs: list[Motif] = []
        for (prev_label, curr_label), count in transitions.items():
            if count < _MIN_TRANSITION_COUNT:
                continue
            total_after_prev = from_prev_total[prev_label]
            # total_after_prev is always >= count (we increment both at
            # the same time), so the division is safe — but guard for
            # the empty/zero case anyway.
            prob = count / total_after_prev if total_after_prev else 0.0
            confidence = min(1.0, prob * (count / 5))
            summary = (
                f"After {prev_label}, user runs {curr_label} "
                f"(n={count}, p={prob:.2f})"
            )
            payload = {
                "prev": prev_label,
                "curr": curr_label,
                "count": count,
                "probability": prob,
            }
            evidence = tuple(evidence_by_pair[(prev_label, curr_label)])
            motifs.append(
                Motif(
                    kind="transition",
                    confidence=confidence,
                    support=count,
                    summary=summary,
                    payload=payload,
                    evidence_event_ids=evidence,
                )
            )
        return motifs


__all__ = ["TransitionChainExtractor"]
