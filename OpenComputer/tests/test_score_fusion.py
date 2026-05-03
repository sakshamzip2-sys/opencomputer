"""P1-3: fused turn_score + judge disagreement detection."""
from __future__ import annotations

from opencomputer.agent.score_fusion import (
    fused_turn_score,
    is_judge_disagreement,
)


def test_fused_when_both_available():
    assert abs(fused_turn_score(0.5, 0.7) - (0.4 * 0.5 + 0.6 * 0.7)) < 1e-9


def test_fused_falls_back_to_composite_when_judge_none():
    """Cost-guard exhaustion or provider missing → composite-only."""
    assert fused_turn_score(0.5, None) == 0.5


def test_disagreement_threshold_default_04():
    assert is_judge_disagreement(0.2, 0.7) is True
    assert is_judge_disagreement(0.5, 0.6) is False
    assert is_judge_disagreement(0.5, None) is False


def test_disagreement_symmetric():
    assert is_judge_disagreement(0.7, 0.2) is True
    assert is_judge_disagreement(0.2, 0.7) is True
