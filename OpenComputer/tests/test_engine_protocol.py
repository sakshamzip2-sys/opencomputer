"""v0.5+ pluggable engine architecture + A/B harness tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from opencomputer.evolution.engine_protocol import (
    EngineRegistry,
    RecommendationEngine,
    default_registry,
    reset_registry,
)
from opencomputer.evolution.policy_engine import MostCitedBelowMedianV1
from opencomputer.evolution.recommendation import NoOpReason, Recommendation


@pytest.fixture(autouse=True)
def _reset_registry_each():
    reset_registry()
    yield
    reset_registry()


class _FakeEngine:
    def __init__(self, version: str):
        self._version = version

    @property
    def version(self) -> str:
        return self._version

    def recommend(self, db) -> Recommendation:
        return Recommendation.noop(NoOpReason.INSUFFICIENT_DATA)


def test_register_real_engine_implements_protocol():
    reg = EngineRegistry()
    eng = MostCitedBelowMedianV1()
    reg.register(eng, default=True)
    assert reg.default() is eng
    assert reg.all_versions() == ["MostCitedBelowMedian/1"]


def test_register_rejects_non_engine_objects():
    reg = EngineRegistry()
    with pytest.raises(TypeError):
        reg.register(object())  # not a RecommendationEngine


def test_default_strategy_returns_default_engine():
    reg = EngineRegistry()
    a = _FakeEngine("A/1")
    b = _FakeEngine("B/1")
    reg.register(a, default=True)
    reg.register(b)
    assert reg.choose() is a


def test_hash_strategy_deterministic_per_knob_per_day():
    """Same (knob_kind, day) hashes to same engine."""
    reg = EngineRegistry()
    reg.register(_FakeEngine("A/1"))
    reg.register(_FakeEngine("B/1"))
    reg.set_strategy("hash")

    pick_1 = reg.choose(knob_kind="recall_penalty")
    pick_2 = reg.choose(knob_kind="recall_penalty")
    assert pick_1 is pick_2  # idempotent within same day


def test_hash_strategy_distributes_across_engines():
    """Different knob_kinds get distributed; same day."""
    reg = EngineRegistry()
    reg.register(_FakeEngine("A/1"))
    reg.register(_FakeEngine("B/1"))
    reg.set_strategy("hash")

    picks = set()
    for kk in ["k1", "k2", "k3", "k4", "k5", "k6"]:
        pick = reg.choose(knob_kind=kk)
        picks.add(pick.version)
    # With 6 different knobs and 2 engines, both should appear
    assert len(picks) == 2


def test_weighted_strategy_respects_weights():
    """Engine with much higher weight gets picked more often
    (deterministic per knob_kind|day)."""
    reg = EngineRegistry()
    reg.register(_FakeEngine("low/1"), weight=1.0)
    reg.register(_FakeEngine("high/1"), weight=100.0)
    reg.set_strategy("weighted")

    picks = []
    for i in range(50):
        pick = reg.choose(knob_kind=f"k{i}")
        picks.append(pick.version)

    high_count = picks.count("high/1")
    # With 100x weight, expect overwhelming majority
    assert high_count > 40


def test_unknown_strategy_raises():
    reg = EngineRegistry()
    with pytest.raises(ValueError):
        reg.set_strategy("totally_made_up")


def test_empty_registry_returns_none():
    reg = EngineRegistry()
    assert reg.choose() is None
    assert reg.default() is None


def test_module_global_registry_bootstrapped_on_engine_tick_import():
    """policy_engine_tick._bootstrap_registry() auto-registers
    MostCitedBelowMedian/1. Idempotent — re-running on an empty
    registry re-populates it."""
    from opencomputer.cron import policy_engine_tick
    from opencomputer.evolution import engine_protocol

    policy_engine_tick._bootstrap_registry()
    # The module-level reset_registry() reassigns the global, so we
    # re-fetch via attribute to see the post-bootstrap instance.
    assert "MostCitedBelowMedian/1" in (
        engine_protocol.default_registry.all_versions()
    )
