"""Novelty judge — Phase 5 stub, Phase 6 lands the real LLM call.

Rule (d) from the plan: when a trace WAS used at pre-task time, run
this judge at session-end to decide whether the agent did something
genuinely novel beyond what the trace showed. If novel → emit a
competing/improving trace; if not → silent.

Phase 5 contract: returns ``False`` unconditionally (treat every
trace-using session as "not novel — silent emit"). That collapses
rule (d) to rule (a) for the trace-was-used branch — which is the
conservative default. Phase 6 swaps the body for a Haiku call with:

* User message (turn 0)
* Agent transcript (assistant + tool messages)
* The trace that was used (intent + insight + steps)

…and returns the LLM's judgement plus a reason string.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_log = logging.getLogger("opencomputer.social_traces.novelty_judge")


@dataclass(frozen=True, slots=True)
class NoveltyVerdict:
    """Result of the LLM judgement (or the Phase 5 stub).

    ``is_novel``: True when the agent improved on the used trace OR
    discovered an edge case. Drives the emit-vs-silent decision.

    ``reason``: short human-readable explanation. Populated by Phase
    6's LLM call; empty in the Phase 5 stub. Used in subscriber
    logs for debuggability — never sent to the network.
    """

    is_novel: bool
    reason: str = ""


async def judge_session_novelty(
    *,
    user_message: str,
    transcript: str,
    used_trace_intent: str,
    used_trace_insight: str,
) -> NoveltyVerdict:
    """Decide whether the session improved on the trace it used.

    Phase 5: STUB. Always returns ``is_novel=False`` so the
    trace-was-used branch never emits. Conservative default — better
    to under-emit than to flood the network with redundant traces
    while the judge prompt is being tuned.

    Phase 6 replaces this body with the real Haiku call (cost-guarded,
    timeout-bounded, prompt versioned in a sibling ``prompts/``
    directory).
    """
    _log.debug(
        "social-traces: novelty_judge stub called — returning is_novel=False "
        "(Phase 6 will swap the implementation)",
    )
    return NoveltyVerdict(is_novel=False, reason="phase-5-stub")


__all__ = ["NoveltyVerdict", "judge_session_novelty"]
