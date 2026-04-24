"""OpenComputer Evolution subpackage — self-improvement via trajectory collection,
reflection, and skill synthesis.

Opt-in (``config.evolution.enabled = False`` by default).  See
``OpenComputer/docs/evolution/`` for architecture design and user guide.

Public surface (B1):

- :data:`SCHEMA_VERSION_CURRENT` — current trajectory schema version constant.
- :class:`TrajectoryEvent` — single (state, action, outcome) tuple.
- :class:`TrajectoryRecord` — session-bounded ordered sequence of events.
- :func:`new_event` — convenience constructor for TrajectoryEvent.
- :func:`new_record` — convenience constructor for an empty TrajectoryRecord.
- :func:`with_event` — immutable append: returns new record with event added.
- :class:`RewardFunction` — Protocol for trajectory reward scoring.
- :class:`RuleBasedRewardFunction` — rule-based MVP reward implementation.
- :class:`Insight` — output of reflection (dataclass shape locked at B1; logic in B2).
- :class:`ReflectionEngine` — STUB: ``reflect()`` raises NotImplementedError until B2.
- :class:`SkillSynthesizer` — STUB: ``synthesize()`` raises NotImplementedError until B2.
"""

from __future__ import annotations

from opencomputer.evolution.reflect import (
    Insight,
    ReflectionEngine,
)
from opencomputer.evolution.reward import (
    RewardFunction,
    RuleBasedRewardFunction,
)
from opencomputer.evolution.synthesize import SkillSynthesizer
from opencomputer.evolution.trajectory import (
    SCHEMA_VERSION_CURRENT,
    TrajectoryEvent,
    TrajectoryRecord,
    new_event,
    new_record,
    with_event,
)

__version__ = "0.1.0"

__all__ = [
    "SCHEMA_VERSION_CURRENT",
    "Insight",
    "ReflectionEngine",
    "RewardFunction",
    "RuleBasedRewardFunction",
    "SkillSynthesizer",
    "TrajectoryEvent",
    "TrajectoryRecord",
    "new_event",
    "new_record",
    "with_event",
]
