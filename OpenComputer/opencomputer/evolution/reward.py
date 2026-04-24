"""Reward function module for OpenComputer Evolution.

Defines the ``RewardFunction`` Protocol and the default ``RuleBasedRewardFunction``
implementation used in the MVP reward pipeline.

Design reference: OpenComputer/docs/evolution/design.md §6.

Privacy rule: this module never reads raw prompt text.  The ``user_confirmed``
signal inspects ``metadata.get("text_starts_with")`` — a short normalized
preview populated by the B3 bus subscriber — never the full message body.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol, runtime_checkable

from opencomputer.evolution.trajectory import TrajectoryEvent, TrajectoryRecord

# ---------------------------------------------------------------------------
# RewardFunction Protocol
# ---------------------------------------------------------------------------

_NEGATIVE_CUES = ("no", "stop", "wrong", "undo", "revert", "cancel")


@runtime_checkable
class RewardFunction(Protocol):
    """Protocol for trajectory reward scoring.

    Reward is in [0.0, 1.0]; higher = better.
    Returns ``None`` for in-flight trajectories (``record.ended_at is None``).
    """

    def score(self, record: TrajectoryRecord) -> float | None: ...


# ---------------------------------------------------------------------------
# Default implementation
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class RuleBasedRewardFunction:
    """Conservative MVP reward: tool success rate + user-confirmed signal + task-completed flag.

    Three weighted signals (all in [0, 1] before weighting):

    | Signal              | Source                                                              | Default weight |
    |---------------------|---------------------------------------------------------------------|----------------|
    | tool_success_rate   | fraction of ``tool_call`` events with outcome == "success"          | 0.5            |
    | user_confirmed      | last user reply does not start with a negative cue                  | 0.3            |
    | task_completed      | record.completion_flag                                              | 0.2            |

    Weights sum to 1.0 by convention (validated in ``__post_init__`` — must sum
    to 1.0 ± 1e-6).

    Anti-gaming notes:
    - No length component (verbose-but-useless responses NOT rewarded).
    - No latency component.
    - LLM-judge reward is explicitly v1.1+; this MVP is rule-based by design.
    """

    weight_tool_success: float = 0.5
    weight_user_confirmed: float = 0.3
    weight_task_completed: float = 0.2

    def __post_init__(self) -> None:
        total = self.weight_tool_success + self.weight_user_confirmed + self.weight_task_completed
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"RuleBasedRewardFunction weights must sum to 1.0 ± 1e-6, got {total}"
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def score(self, record: TrajectoryRecord) -> float | None:
        """Score a trajectory record.

        Returns:
            ``None`` if the session is still in-flight (``ended_at is None``).
            ``0.0`` if the record has no events.
            A float in [0.0, 1.0] otherwise.
        """
        if record.ended_at is None:
            return None

        tool_success = self._tool_success_rate(record.events)
        user_conf = self._user_confirmed(record.events)
        task_done = 1.0 if record.completion_flag is True else 0.0

        raw = (
            self.weight_tool_success * tool_success
            + self.weight_user_confirmed * user_conf
            + self.weight_task_completed * task_done
        )
        return max(0.0, min(1.0, raw))

    # ------------------------------------------------------------------
    # Signal helpers (private)
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_success_rate(events: tuple[TrajectoryEvent, ...]) -> float:
        """Fraction of tool_call events with outcome == 'success'.

        Returns 0.0 if there are no tool_call events.
        """
        tool_events = [e for e in events if e.action_type == "tool_call"]
        if not tool_events:
            return 0.0
        successes = sum(1 for e in tool_events if e.outcome == "success")
        return successes / len(tool_events)

    @staticmethod
    def _user_confirmed(events: tuple[TrajectoryEvent, ...]) -> float:
        """Score the last user_reply event's text preview.

        Signal values:
        - 0.5  — no user_reply event found, or text_starts_with key absent (neutral)
        - 0.0  — text_starts_with (lowercased, stripped) starts with a negative cue
        - 1.0  — text_starts_with present and starts with no negative cue

        Privacy: reads ``metadata["text_starts_with"]`` only — never raw prompt text.
        """
        # Find the last user_reply event.
        last_user_reply: TrajectoryEvent | None = None
        for event in reversed(events):
            if event.action_type == "user_reply":
                last_user_reply = event
                break

        if last_user_reply is None:
            return 0.0

        text_preview = last_user_reply.metadata.get("text_starts_with")
        if text_preview is None:
            return 0.5

        normalized = str(text_preview).lower().strip()
        for cue in _NEGATIVE_CUES:
            if normalized.startswith(cue):
                return 0.0

        return 1.0
