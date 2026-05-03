"""Pluggable recommendation-engine protocol.

Closes the v0.5 deferral that v0 had exactly one engine
(``MostCitedBelowMedian/1``) and any future engine required surgery
on policy_engine_tick. With this protocol, engines:

  - Implement ``recommend(db) -> Recommendation`` (already the
    interface MostCitedBelowMedian/1 follows)
  - Expose ``version`` (string used as ``recommendation_engine_version``
    in policy_changes for cohort audit)
  - Register via :class:`EngineRegistry`

The cron's policy_engine_tick picks an engine via:

  - Single-engine deployments: registry.default()
  - A/B deployments: registry.choose(strategy="round_robin" | "weighted")

A/B strategy uses a hash of (knob_kind, day) so the same knob_kind on
the same day always sees the same engine — avoids two engines
hammering each other's recommendations within a budget window.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from opencomputer.evolution.recommendation import Recommendation


@runtime_checkable
class RecommendationEngine(Protocol):
    @property
    def version(self) -> str:
        """e.g. ``MostCitedBelowMedian/1`` — written verbatim into
        policy_changes.recommendation_engine_version."""
        ...

    def recommend(self, db) -> Recommendation:
        """Inspect the DB; return a Recommendation or
        Recommendation.noop(...) if no candidate."""
        ...


@dataclass
class EngineRegistry:
    """Module-global registry of engines + A/B strategy state."""

    _engines: dict[str, RecommendationEngine] = field(default_factory=dict)
    _weights: dict[str, float] = field(default_factory=dict)
    _strategy: str = "default"
    _default_version: str | None = None

    def register(
        self, engine: RecommendationEngine, *,
        weight: float = 1.0, default: bool = False,
    ) -> None:
        """Register an engine. ``weight`` is used by 'weighted' A/B
        strategy. ``default=True`` makes it the fallback for the
        single-engine path."""
        if not isinstance(engine, RecommendationEngine):
            raise TypeError(
                "engine must implement RecommendationEngine protocol"
            )
        self._engines[engine.version] = engine
        self._weights[engine.version] = weight
        if default or self._default_version is None:
            self._default_version = engine.version

    def get(self, version: str) -> RecommendationEngine | None:
        return self._engines.get(version)

    def all_versions(self) -> list[str]:
        return sorted(self._engines.keys())

    def default(self) -> RecommendationEngine | None:
        if self._default_version is None:
            return None
        return self._engines.get(self._default_version)

    def set_strategy(self, strategy: str) -> None:
        if strategy not in ("default", "round_robin", "weighted", "hash"):
            raise ValueError(f"unknown strategy: {strategy}")
        self._strategy = strategy

    def choose(self, *, knob_kind: str = "") -> RecommendationEngine | None:
        """Pick an engine for this tick. Same (knob_kind, day) → same
        engine within a 24h window."""
        if not self._engines:
            return None
        if self._strategy == "default" or len(self._engines) == 1:
            return self.default()
        if self._strategy in ("hash", "round_robin"):
            today = datetime.now().strftime("%Y-%m-%d")
            seed = hashlib.sha256(
                f"{knob_kind}|{today}".encode()
            ).hexdigest()
            idx = int(seed[:8], 16) % len(self._engines)
            return self._engines[sorted(self._engines.keys())[idx]]
        if self._strategy == "weighted":
            # Cumulative-weight selection by hash. Deterministic per day.
            today = datetime.now().strftime("%Y-%m-%d")
            seed = int(hashlib.sha256(
                f"{knob_kind}|{today}".encode()
            ).hexdigest()[:8], 16)
            total = sum(self._weights.values())
            target = (seed % 1000) / 1000.0 * total
            running = 0.0
            for v in sorted(self._engines.keys()):
                running += self._weights[v]
                if running >= target:
                    return self._engines[v]
            return self.default()
        return self.default()


#: Module-global registry. Populated at import-time of policy_engine_tick.
default_registry = EngineRegistry()


def reset_registry() -> None:
    """Test fixture hook — wipes registry state."""
    global default_registry
    default_registry = EngineRegistry()
