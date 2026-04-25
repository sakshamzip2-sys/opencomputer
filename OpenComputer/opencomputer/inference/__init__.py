"""
Behavioral inference engine (Phase 3.B, F2 continued).

Subscribes to :data:`opencomputer.ingestion.bus.default_bus`, runs the
three SDK :class:`plugin_sdk.inference.MotifExtractor` implementations
over event batches, and persists the resulting :class:`plugin_sdk.inference.Motif`
records to a SQLite store at ``<profile_home>/inference/motifs.sqlite``.

Phase 3.C user-model graph reads from :class:`MotifStore.list` — that's
the public consumption path for downstream consumers.
"""

from opencomputer.inference.engine import BehavioralInferenceEngine
from opencomputer.inference.extractors import (
    ImplicitGoalExtractor,
    TemporalMotifExtractor,
    TransitionChainExtractor,
)
from opencomputer.inference.storage import MotifStore

__all__ = [
    "BehavioralInferenceEngine",
    "ImplicitGoalExtractor",
    "MotifStore",
    "TemporalMotifExtractor",
    "TransitionChainExtractor",
]
