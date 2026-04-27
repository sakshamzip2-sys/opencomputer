"""Life-event pattern framework.

A LifeEventPattern observes events on the F2 SignalEvent bus, accumulates
evidence in a sliding window, and fires when confidence crosses threshold.

Patterns split into two surfacing policies:
- ``surfacing="hint"`` — fires a chat-context hint at next turn ("noticed
  your work rhythm shifted — anything you want to talk about?")
- ``surfacing="silent"`` — writes an F4 user-model edge with low confidence
  but never surfaces in chat (HealthEvent, RelationshipShift). The agent's
  responses subtly adjust tone but never name the inference.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

SurfacingPolicy = Literal["hint", "silent"]


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One observation contributing to a pattern's confidence."""

    timestamp: float
    weight: float  # 0.0..1.0
    source: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PatternFiring:
    """A pattern crossed its surface threshold."""

    pattern_id: str
    confidence: float
    evidence_count: int
    surfacing: SurfacingPolicy
    hint_text: str = ""
    timestamp: float = field(default_factory=time.time)


class LifeEventPattern(ABC):
    """Subscribers extend this. Default sliding window = 14 days, decay = exp(-age/7d)."""

    pattern_id: str = ""
    surface_threshold: float = 0.7
    surfacing: SurfacingPolicy = "hint"
    window_days: float = 14.0
    decay_half_life_days: float = 7.0

    def __init__(self) -> None:
        self._evidence: list[EvidenceItem] = []

    @abstractmethod
    def consider_event(self, event_type: str, metadata: dict[str, object]) -> EvidenceItem | None:
        """Inspect a SignalEvent. Return EvidenceItem if relevant; None to skip."""

    def accumulate(self, event_type: str, metadata: dict[str, object]) -> PatternFiring | None:
        item = self.consider_event(event_type, metadata)
        if item is None:
            return None
        self._evidence.append(item)
        self._prune()
        confidence = self._compute_confidence()
        if confidence < self.surface_threshold:
            return None
        return PatternFiring(
            pattern_id=self.pattern_id,
            confidence=confidence,
            evidence_count=len(self._evidence),
            surfacing=self.surfacing,
            hint_text=self.hint_text(),
        )

    def _prune(self) -> None:
        cutoff = time.time() - (self.window_days * 86400)
        self._evidence = [e for e in self._evidence if e.timestamp >= cutoff]

    def _compute_confidence(self) -> float:
        """Decay-weighted sum of evidence weights, capped at 1.0."""
        if not self._evidence:
            return 0.0
        now = time.time()
        decay = self.decay_half_life_days * 86400
        weighted = sum(
            e.weight * (0.5 ** ((now - e.timestamp) / decay))
            for e in self._evidence
        )
        return min(1.0, weighted)

    def hint_text(self) -> str:
        return ""
