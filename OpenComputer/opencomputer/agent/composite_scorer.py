"""Phase 1 composite scorer — pure-arithmetic signal fusion.

Combines Phase 0 implicit signals into a single composite_score in
[0, 1]. No LLM call. Designed to give a useful score even when the user
is silent (baseline 0.5 anchors the absent-signal case).

Weights tuned to prevent reward-hacking patterns (see spec section
"Reward-Hacking Traps"):
- tool_success_rate alone is capped at 0.20 — agent can't game by avoiding tools.
- correction always penalises more than affirmation rewards (anti-sycophancy).
- conversation_abandoned counts only as a modest negative; the spec
  notes that a follow-up audit may treat abandonment-WITH-friction more
  harshly, but we keep the standalone weight low so trailing
  "anything else?" prompts don't pay off.
"""
from __future__ import annotations


def _normalize(value: int, max_val: int) -> float:
    """Linear normalize to [0, 1], saturating at max_val."""
    if value <= 0:
        return 0.0
    return min(1.0, value / max_val)


def compute_composite_score(
    *,
    tool_call_count: int,
    tool_success_count: int,
    tool_error_count: int,
    self_cancel_count: int,
    retry_count: int,
    conversation_abandoned: bool,
    affirmation_present: bool,
    correction_present: bool,
    vibe_delta: int,                    # +1 improved, -1 degraded, 0 same
    standing_order_violation_count: int,
) -> float:
    """Return composite score in [0, 1].

    Baseline 0.5 ensures silent turns aren't crashed to zero.
    Tool success is the largest positive signal.
    Correction is the largest negative signal (with self-cancel close behind).
    """
    score = 0.50  # baseline so silence doesn't crash to zero

    # Tool success rate (positive, capped 0.20)
    denom = tool_success_count + tool_error_count + 1
    tool_success_rate = tool_success_count / denom
    score += 0.20 * tool_success_rate

    # Self-cancel and retry (negative)
    score -= 0.15 * _normalize(self_cancel_count, max_val=2)
    score -= 0.15 * _normalize(retry_count, max_val=3)

    # Abandonment (negative, modest — anti-"anything else?" pattern)
    if conversation_abandoned:
        score -= 0.10

    # Affirmation/correction (asymmetric: correction hurts more than affirmation helps)
    if affirmation_present:
        score += 0.10
    if correction_present:
        score -= 0.15

    # Vibe delta (small effect)
    score += 0.05 * vibe_delta

    # Standing-order violations (modest negative)
    score -= 0.10 * _normalize(standing_order_violation_count, max_val=3)

    return max(0.0, min(1.0, score))
