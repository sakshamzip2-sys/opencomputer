"""
Implicit-goal extractor — top-N tool sequence per session (heuristic).

Groups events by ``session_id``; sessions with fewer than 3 distinct
tool names are skipped (too sparse to characterise). For each surviving
session, emits one :class:`plugin_sdk.inference.Motif` summarising the
top 5 most-used tools.

This is a heuristic-only stub at Phase 3.B — the
:class:`plugin_sdk.inference.MotifExtractor` Protocol stays the same
when the implementation is later swapped for an LLM-judged variant
(e.g. "given the tools used, what was the user trying to do?").
Phase 3.C user-model graph reads :class:`Motif.payload['top_tools']`
without caring whether the producer was heuristic or LLM-backed.

Privacy posture
---------------

Tool names + counts only. The session_id is included for downstream
correlation but no message bodies, file paths, or web URLs are
embedded in the motif.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import ClassVar

from plugin_sdk.inference import Motif, MotifKind
from plugin_sdk.ingestion import SignalEvent, ToolCallEvent

#: Sessions with fewer than this many distinct tools are not summarised.
#: Single-tool sessions ("user just opened the agent and asked one
#: question") have no useful goal pattern to extract.
_MIN_DISTINCT_TOOLS = 3

#: Top-N most-used tools to include in the summary payload.
_TOP_N_TOOLS = 5


class ImplicitGoalExtractor:
    """Heuristic implicit-goal summariser (one motif per session).

    Implements :class:`plugin_sdk.inference.MotifExtractor`. Pure.
    """

    name: ClassVar[str] = "implicit_goal"
    kind: ClassVar[MotifKind] = "implicit_goal"

    def extract(self, events: Sequence[SignalEvent]) -> list[Motif]:
        """Emit one motif per session with >= 3 distinct tool names.

        Confidence formula: ``min(1.0, 0.3 + 0.1 * d)`` where ``d`` is
        the number of distinct tools. A session with 3 distinct tools
        is at 0.6; 7+ distinct tools saturate the confidence at 1.0.
        Pure heuristic — production calibration is Phase 3.C+ work.
        """
        # Group events by session_id. Events without a session are
        # dropped — implicit goals are inherently a per-session concept.
        by_session: dict[str, list[SignalEvent]] = defaultdict(list)
        for event in events:
            if event.session_id is None:
                continue
            # Only ToolCallEvent contributes to "tool sequence". Other
            # event types (web observations, file ops, messages) don't
            # carry a tool_name field, and the extractor would emit an
            # uninformative summary if we tried to coerce them.
            if not isinstance(event, ToolCallEvent):
                continue
            if not event.tool_name:
                continue
            by_session[event.session_id].append(event)

        motifs: list[Motif] = []
        for session_id, session_events in by_session.items():
            tool_names = [e.tool_name for e in session_events]
            distinct = len(set(tool_names))
            if distinct < _MIN_DISTINCT_TOOLS:
                continue
            counter = Counter(tool_names)
            top_5 = [name for name, _ in counter.most_common(_TOP_N_TOOLS)]
            n = len(session_events)
            confidence = min(1.0, 0.3 + 0.1 * distinct)
            summary = (
                f"Session {session_id[:8]}…: tool sequence suggests goal — "
                f"{', '.join(top_5)}"
            )
            payload = {
                "session_id": session_id,
                "top_tools": top_5,
                "n_events": n,
                "n_distinct_tools": distinct,
            }
            evidence = tuple(e.event_id for e in session_events)
            motifs.append(
                Motif(
                    kind="implicit_goal",
                    confidence=confidence,
                    support=n,
                    summary=summary,
                    payload=payload,
                    evidence_event_ids=evidence,
                    session_id=session_id,
                )
            )
        return motifs


__all__ = ["ImplicitGoalExtractor"]
