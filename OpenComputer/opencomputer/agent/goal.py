"""Persistent cross-turn goals — the Ralph loop.

A standing-goal slash command keeps the agent working toward a stated
objective across turns until it is satisfied (auxiliary-model judge),
paused, or the turn budget runs out.

Mirrors hermes-agent ``265bd59c1``. Adapted to OC's flat layout:
- :class:`GoalState` is the in-memory shape (immutable view of DB row).
- Persistence: schema v11 columns on the ``sessions`` table
  (``goal_text``, ``goal_active``, ``goal_turns_used``, ``goal_budget``);
  CRUD methods live on :class:`opencomputer.agent.state.SessionDB`
  (``set_session_goal`` / ``get_session_goal`` / ``update_session_goal`` /
  ``clear_session_goal``).
- Judge call uses :func:`opencomputer.agent.aux_llm.complete_text` —
  failure is treated as "not satisfied" so a flaky judge never wedges
  the loop.

Continuation-loop integration (injecting the next user message at end of
turn) is wired into :func:`opencomputer.agent.loop.AgentLoop.run_conversation`
in a follow-up commit. This module exposes the building blocks; the wiring
is intentionally additive so the loop stays revertable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Default turn budget — judge runs after each assistant turn; if not
#: satisfied AND ``turns_used < budget`` the loop synthesizes a
#: continuation prompt.
DEFAULT_BUDGET: int = 20


@dataclass(slots=True, frozen=True)
class GoalState:
    """In-memory view of a session's goal row."""

    text: str
    active: bool = True
    turns_used: int = 0
    budget: int = DEFAULT_BUDGET

    def should_continue(self) -> bool:
        """True if the loop should synthesize another continuation prompt."""
        return self.active and self.turns_used < self.budget


CONTINUATION_PROMPT_TEMPLATE = (
    "(continuing toward goal: {goal_text})\n"
    "Take the next concrete step. If the goal is complete, say so explicitly."
)


def build_continuation_prompt(goal_text: str) -> str:
    """The user-role message injected at end-of-turn when the goal is unmet."""
    return CONTINUATION_PROMPT_TEMPLATE.format(goal_text=goal_text)


JUDGE_SYSTEM_PROMPT = (
    "You are a strict goal-satisfaction judge. The user set a standing goal "
    "and the assistant just produced a response. Determine whether the goal "
    "is now satisfied. Respond with ONLY one of: SATISFIED, NOT_SATISFIED."
)

JUDGE_USER_TEMPLATE = "Goal: {goal_text}\n\nLast assistant response:\n{last_response}"


async def _call_judge_model(prompt: str) -> str:
    """Invoke the auxiliary model. Wrapped so tests can monkeypatch."""
    from opencomputer.agent.aux_llm import complete_text

    return await complete_text(
        messages=[{"role": "user", "content": prompt}],
        system=JUDGE_SYSTEM_PROMPT,
        max_tokens=8,
        temperature=0.0,
    )


async def judge_satisfied(*, goal_text: str, last_response: str) -> bool:
    """Returns True iff the goal is satisfied. Fails OPEN on any judge error.

    Fail-open semantics matter: a flaky judge model must never wedge a
    user's progress. The turn budget on :class:`GoalState` is the real
    backstop — the judge is just an early-exit signal.
    """
    if not last_response:
        return False
    try:
        prompt = JUDGE_USER_TEMPLATE.format(
            goal_text=goal_text, last_response=last_response[:4000]
        )
        verdict = (await _call_judge_model(prompt)).strip().upper()
    except Exception as exc:  # noqa: BLE001 — fail-open is the design
        logger.warning("goal judge call failed (failing open): %s", exc)
        return False
    if not verdict:
        return False
    if "NOT" in verdict:
        return False
    return verdict.startswith("SATISFIED")
