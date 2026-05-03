"""Phase 1 fused score — combines composite + judge into final turn_score.

Judge dominates (0.6 weight) because it can read trajectory semantics
(e.g., the *severity* of a correction needs LLM context). Composite
(0.4 weight) anchors when the judge has bias.

When the judge is unavailable (cost guard exhausted, provider missing,
parsing failure), turn_score falls back to composite alone — graceful
degradation per the always-on data flow constraint.
"""
from __future__ import annotations

_DISAGREEMENT_THRESHOLD = 0.4


def fused_turn_score(
    composite_score: float, judge_score: float | None,
) -> float:
    """Final turn_score in [0, 1].

    composite-only when judge_score is None (cost guard, missing provider,
    parse failure). Otherwise weighted combination.
    """
    if judge_score is None:
        return composite_score
    return 0.4 * composite_score + 0.6 * judge_score


def is_judge_disagreement(composite: float, judge: float | None) -> bool:
    """Flag turns where composite and judge diverge significantly.

    Surfaces signal-vs-LLM disagreement for human review — may indicate
    weight mis-calibration or judge-prompt drift over time.
    """
    if judge is None:
        return False
    return abs(composite - judge) > _DISAGREEMENT_THRESHOLD
