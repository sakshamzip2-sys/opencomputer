"""Reflection engine and Insight dataclass for OpenComputer Evolution.

``Insight`` is the output of the reflection engine — one observation about
the agent's behaviour with a proposed action.  The dataclass shape is locked
at B1 so that storage, CLI, and the synthesizer can be wired against a stable
contract.  The ``ReflectionEngine`` logic itself lands in B2.

Design reference: OpenComputer/docs/evolution/design.md §4.3 and §7.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any, Literal

from opencomputer.evolution.trajectory import TrajectoryRecord

# ---------------------------------------------------------------------------
# Valid action_type values (checked at runtime — Literal isn't enforced by Python)
# ---------------------------------------------------------------------------

_VALID_ACTION_TYPES: frozenset[str] = frozenset({"create_skill", "edit_prompt", "noop"})


# ---------------------------------------------------------------------------
# Insight dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Insight:
    """Output of reflection — one observation about the agent's behavior with a proposed action.

    Implementation logic lands in B2.  This dataclass shape is locked at B1 so consumers
    (storage / CLI / synthesis) can be wired against a stable contract.
    """

    observation: str
    """Human-readable summary of the observed pattern."""

    evidence_refs: tuple[int, ...]
    """Trajectory record ids supporting the observation.  Must be a tuple, not a list."""

    action_type: Literal["create_skill", "edit_prompt", "noop"]
    """Proposed action category."""

    payload: Mapping[str, Any]
    """Action-specific detail (slug, draft text, diff, etc.)."""

    confidence: float
    """Confidence score in [0.0, 1.0]."""

    def __post_init__(self) -> None:
        # Validate confidence is in the unit interval.
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"Insight.confidence must be in [0.0, 1.0], got {self.confidence!r}"
            )

        # Validate evidence_refs is a tuple (not a list).
        if not isinstance(self.evidence_refs, tuple):
            raise TypeError(
                f"Insight.evidence_refs must be a tuple, got {type(self.evidence_refs).__name__}"
            )

        # Validate action_type (Literal is not enforced at runtime by Python).
        if self.action_type not in _VALID_ACTION_TYPES:
            raise ValueError(
                f"Insight.action_type must be one of {sorted(_VALID_ACTION_TYPES)!r}, "
                f"got {self.action_type!r}"
            )


# ---------------------------------------------------------------------------
# ReflectionEngine (B1 stub)
# ---------------------------------------------------------------------------


class ReflectionEngine:
    """GEPA-style reflection engine — analyses trajectory batches, proposes Insights.

    B1: stub only — ``reflect()`` raises NotImplementedError.  The constructor accepts
    the parameters that B2 will need (provider + window) so callers can be written
    against a stable signature today.
    """

    def __init__(
        self,
        *,
        provider: Any,
        window: int = 30,
    ) -> None:
        """Initialise the engine.

        Args:
            provider: LLM provider instance.  B2 will tighten the type to
                ``BaseProvider`` from ``plugin_sdk.provider_contract``; B1 accepts
                ``Any`` to avoid prematurely locking that import.
            window: Maximum number of trajectory records to pass to a single
                ``reflect()`` call.  Must be >= 1.

        Raises:
            ValueError: if *window* < 1.
        """
        if window < 1:
            raise ValueError("window must be >= 1")
        self._provider = provider
        self._window = window

    @property
    def window(self) -> int:
        """Maximum batch size for ``reflect()`` calls."""
        return self._window

    def reflect(self, records: list[TrajectoryRecord]) -> list[Insight]:
        """Analyse a batch of completed trajectories.  Returns Insights.

        B1: NotImplementedError.  B2 implementation: render Jinja2 prompt at
        ``opencomputer/evolution/prompts/reflect.j2``, call provider, parse JSON output.

        Raises:
            NotImplementedError: always in B1.
        """
        raise NotImplementedError("ReflectionEngine.reflect() lands in B2 — see plan §B2")
