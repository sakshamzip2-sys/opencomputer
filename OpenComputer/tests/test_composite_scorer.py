"""P1-2: composite scorer — pure-arithmetic Phase 0 signal fusion."""
from __future__ import annotations

from opencomputer.agent.composite_scorer import compute_composite_score


def _kwargs(**overrides):
    base = dict(
        tool_call_count=0,
        tool_success_count=0,
        tool_error_count=0,
        self_cancel_count=0,
        retry_count=0,
        conversation_abandoned=False,
        affirmation_present=False,
        correction_present=False,
        vibe_delta=0,
        standing_order_violation_count=0,
    )
    base.update(overrides)
    return base


def test_baseline_silent_turn_returns_baseline():
    """Silent turn → baseline 0.5; user silence isn't a failure signal."""
    score = compute_composite_score(**_kwargs())
    assert 0.45 < score < 0.55


def test_perfect_turn_caps_at_1():
    score = compute_composite_score(**_kwargs(
        tool_call_count=3,
        tool_success_count=3,
        affirmation_present=True,
        vibe_delta=1,
    ))
    assert score >= 0.7
    assert score <= 1.0


def test_terrible_turn_floors_at_0():
    score = compute_composite_score(**_kwargs(
        tool_call_count=4,
        tool_error_count=4,
        self_cancel_count=2,
        retry_count=3,
        conversation_abandoned=True,
        correction_present=True,
        vibe_delta=-1,
        standing_order_violation_count=3,
    ))
    assert score <= 0.2
    assert score >= 0.0


def test_correction_subtracts_more_than_affirmation_adds():
    """Reward-hacking defense: sycophancy-fishing must not pay off.
    A correction must hurt the score more than a thank-you helps it."""
    base = compute_composite_score(**_kwargs())
    after_correct = compute_composite_score(**_kwargs(correction_present=True))
    after_affirm = compute_composite_score(**_kwargs(affirmation_present=True))
    correction_drop = base - after_correct
    affirmation_gain = after_affirm - base
    assert correction_drop > affirmation_gain


def test_score_bounded_to_unit_interval():
    """Even with extreme values the score stays in [0, 1]."""
    high = compute_composite_score(**_kwargs(
        tool_call_count=100,
        tool_success_count=100,
        affirmation_present=True,
        vibe_delta=1,
    ))
    low = compute_composite_score(**_kwargs(
        tool_error_count=100,
        self_cancel_count=100,
        retry_count=100,
        correction_present=True,
        conversation_abandoned=True,
        vibe_delta=-1,
        standing_order_violation_count=100,
    ))
    assert 0.0 <= high <= 1.0
    assert 0.0 <= low <= 1.0
