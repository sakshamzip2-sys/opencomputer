"""Phase 2 v0 recommendation dataclass.

Engines emit a Recommendation per nightly run. ``Recommendation.is_noop()``
distinguishes "no signal strong enough" from "no eligible candidates."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class NoOpReason(str, Enum):
    NO_CANDIDATE_BELOW_THRESHOLD = "no_candidate_below_threshold"
    BUDGET_EXHAUSTED = "budget_exhausted"
    KILL_SWITCH = "kill_switch"
    INSUFFICIENT_DATA = "insufficient_data"
    ALL_CANDIDATES_IN_COOLDOWN = "all_candidates_in_cooldown"


@dataclass(frozen=True, slots=True)
class Recommendation:
    knob_kind: str
    target_id: str
    prev_value: dict = field(default_factory=dict)
    new_value: dict = field(default_factory=dict)
    reason: str = ""
    expected_effect: str = ""
    engine_version: str = ""
    rollback_hook: dict = field(default_factory=dict)
    noop_reason: NoOpReason | None = None

    def is_noop(self) -> bool:
        return self.noop_reason is not None

    @classmethod
    def noop(cls, reason: NoOpReason) -> "Recommendation":
        return cls(
            knob_kind="",
            target_id="",
            noop_reason=reason,
        )
