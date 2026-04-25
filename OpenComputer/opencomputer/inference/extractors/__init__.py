"""Concrete :class:`plugin_sdk.inference.MotifExtractor` implementations.

Three heuristic extractors ship at Phase 3.B:

* :class:`TemporalMotifExtractor` — recurring (hour, weekday) buckets
* :class:`TransitionChainExtractor` — adjacent-event follow-on counts
* :class:`ImplicitGoalExtractor` — top-N tool sequence per session

The :class:`plugin_sdk.inference.MotifExtractor` Protocol stays the
same when the implementation is later swapped for an LLM-judged
variant, so :class:`opencomputer.inference.engine.BehavioralInferenceEngine`
and Phase 3.C's user-model graph keep working unchanged.
"""

from opencomputer.inference.extractors.implicit_goals import ImplicitGoalExtractor
from opencomputer.inference.extractors.temporal_motifs import TemporalMotifExtractor
from opencomputer.inference.extractors.transition_chains import (
    TransitionChainExtractor,
)

__all__ = [
    "ImplicitGoalExtractor",
    "TemporalMotifExtractor",
    "TransitionChainExtractor",
]
