"""Phase 1 LLM-judge for per-turn quality verdict.

Calls a cheap LLM (Haiku) with the turn's trajectory + composite signal
score + standing orders. Parses ``<judge_score>X</judge_score>`` plus
optional ``<reasoning>...</reasoning>`` from the response.

Failure modes (any → ``None``, caller falls back to composite-only):
- provider.complete raises
- response unparseable (no <judge_score> tag, or bad float)
- score out of [0, 1]

Cost guard wiring lives in the caller (PostResponseReviewer extension);
this module is provider-agnostic so swapping the judge model is one line.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

_logger = logging.getLogger(__name__)

_SCORE_RE = re.compile(r"<judge_score>\s*([0-9.]+)\s*</judge_score>", re.IGNORECASE)
_REASONING_RE = re.compile(
    r"<reasoning>\s*(.*?)\s*</reasoning>", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    judge_score: float
    judge_reasoning: str
    judge_model: str


_JUDGE_PROMPT = """You are evaluating a single turn of an AI assistant.

The assistant's behavior in this turn:
{trajectory_summary}

Composite signal score (computed from tool success, user reaction, etc.):
{composite_score:.2f}

Standing orders the assistant should follow:
{standing_orders}

Rate how well this turn served the user, on a scale of 0.0 to 1.0:
- 0.0 = Completely failed (wrong action, broke standing orders, harmful)
- 0.5 = Neutral / partial success
- 1.0 = Excellent (correct action, user goal advanced, no friction)

Respond in this exact format:
<judge_score>0.XX</judge_score>
<reasoning>Brief 1-2 sentence justification.</reasoning>
"""


async def score_turn_via_judge(
    *,
    provider,
    model: str,
    trajectory_summary: str,
    composite_score: float,
    standing_orders: str,
) -> JudgeVerdict | None:
    """Call cheap LLM to score this turn. Returns None on any failure path."""
    prompt = _JUDGE_PROMPT.format(
        trajectory_summary=trajectory_summary,
        composite_score=composite_score,
        standing_orders=standing_orders or "(none specified)",
    )
    try:
        response = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
    except Exception as e:  # noqa: BLE001 — judge must never break the loop
        _logger.warning("judge LLM call failed: %s", e)
        return None

    text = getattr(response, "text", "") or ""
    score_match = _SCORE_RE.search(text)
    if not score_match:
        _logger.warning("judge response unparseable: %r", text[:100])
        return None
    try:
        score = float(score_match.group(1))
    except ValueError:
        return None
    if not (0.0 <= score <= 1.0):
        return None

    reason_match = _REASONING_RE.search(text)
    reasoning = reason_match.group(1) if reason_match else ""
    return JudgeVerdict(
        judge_score=score, judge_reasoning=reasoning, judge_model=model,
    )
