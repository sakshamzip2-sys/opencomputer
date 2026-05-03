"""P2-4: recall_penalty decay function + multiplicative scoring."""
from __future__ import annotations

from opencomputer.agent.recall_synthesizer import (
    apply_recall_penalty,
    decay_factor,
)


def test_decay_factor_at_age_zero_is_one():
    assert abs(decay_factor(age_days=0) - 1.0) < 1e-9


def test_decay_factor_decays_over_time():
    f0 = decay_factor(age_days=0)
    f30 = decay_factor(age_days=30)
    f60 = decay_factor(age_days=60)
    assert f0 > f30 > f60


def test_decay_after_60_days_below_5_percent():
    """60-day decay should leave at most 5% of original effect."""
    assert decay_factor(age_days=60) < 0.05


def test_apply_recall_penalty_floors_at_005():
    raw = 1.0
    adjusted = apply_recall_penalty(raw, recall_penalty=0.99, age_days=0)
    assert adjusted >= 0.05


def test_apply_recall_penalty_zero_penalty_is_identity():
    assert apply_recall_penalty(0.7, recall_penalty=0.0, age_days=0) == 0.7


def test_apply_recall_penalty_decays_back_to_neutral():
    """A 0.5 penalty applied 60 days ago has near-no effect today."""
    aged = apply_recall_penalty(1.0, recall_penalty=0.5, age_days=60)
    fresh = apply_recall_penalty(1.0, recall_penalty=0.5, age_days=0)
    assert aged > fresh
    assert aged > 0.9  # close to 1.0 (no penalty)


def test_negative_age_clamped_to_zero():
    """Clock-skew safety: negative age treated as 0 (no decay applied)."""
    assert decay_factor(age_days=-5) == 1.0
