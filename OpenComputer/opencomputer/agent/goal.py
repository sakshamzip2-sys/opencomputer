"""Persistent cross-turn goals — the Ralph loop.

A standing-goal slash command keeps the agent working toward a stated
objective across turns until it is satisfied (auxiliary-model judge),
paused, or the turn budget runs out.

Mirrors hermes-agent ``265bd59c1`` and the v2 spec
(``docs/superpowers/specs/2026-05-08-kanban-goals-v2-design.md``):

- :class:`GoalState` is the in-memory shape (immutable view of DB row),
  including ``last_judge_reason`` (the most recent judge rationale).
- :class:`JudgeVerdict` is the strict-JSON judge response: ``done`` drives
  the continuation gate; ``reason`` drives the live banner UX.
- Persistence: schema v14 columns on the ``sessions`` table
  (``goal_text``, ``goal_active``, ``goal_turns_used``, ``goal_budget``,
  ``goal_last_judge_reason``); CRUD methods live on
  :class:`opencomputer.agent.state.SessionDB`.
- Judge call: routes through ``auxiliary.goal_judge`` (provider/model)
  when configured, else falls back to
  :func:`opencomputer.agent.aux_llm.complete_text`. Always fail-open — a
  flaky judge must never wedge user progress; the turn budget is the
  real backstop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Default turn budget — judge runs after each assistant turn; if not
#: satisfied AND ``turns_used < budget`` the loop synthesizes a
#: continuation prompt. Runtime resolves the live default from
#: :class:`opencomputer.agent.config.GoalsConfig`; this constant remains
#: as a fallback for callers that build :class:`GoalState` from raw DB
#: rows that pre-date config wiring.
DEFAULT_BUDGET: int = 20


@dataclass(slots=True, frozen=True)
class GoalState:
    """In-memory view of a session's goal row.

    ``last_judge_reason`` is the most recent rationale the judge returned;
    rendered by ``/goal status`` and the loop's continuation banner. Old
    rows pre-dating schema v14 read it as ``None``.
    """

    text: str
    active: bool = True
    turns_used: int = 0
    budget: int = DEFAULT_BUDGET
    last_judge_reason: str | None = None

    def should_continue(self) -> bool:
        """True if the loop should synthesize another continuation prompt."""
        return self.active and self.turns_used < self.budget

    def budget_exhausted(self) -> bool:
        """True when an active goal has hit its turn budget."""
        return self.active and self.turns_used >= self.budget


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
    "is now satisfied OR unachievable/blocked (treat blocked as done — to "
    "avoid burning the turn budget on impossible tasks). "
    "Respond with STRICT JSON ONLY, no prose, no markdown fences:\n"
    '{"done": <true|false>, "reason": "<one short sentence>"}'
)

JUDGE_USER_TEMPLATE = "Goal: {goal_text}\n\nLast assistant response:\n{last_response}"


@dataclass(slots=True, frozen=True)
class JudgeVerdict:
    """Structured judge response. ``done`` drives the loop; ``reason`` UX."""

    done: bool
    reason: str


_FENCE_PREFIXES: tuple[str, ...] = ("```json", "```JSON", "```")


def _strip_fences(text: str) -> str:
    """Strip leading/trailing markdown code fences if the judge wrapped JSON."""
    s = text.strip()
    for prefix in _FENCE_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix) :].lstrip("\n").strip()
            break
    if s.endswith("```"):
        s = s[:-3].rstrip()
    return s


def _unparseable() -> JudgeVerdict:
    return JudgeVerdict(done=False, reason="(judge response unparseable)")


async def _call_judge_model(prompt: str) -> str:
    """Invoke the auxiliary model.

    Routes through ``auxiliary.goal_judge`` (provider + model) when both
    fields are configured; otherwise falls back to the chat provider via
    :func:`opencomputer.agent.aux_llm.complete_text`. Wrapped so tests can
    monkeypatch one entry point.
    """
    from opencomputer.agent.config import default_config

    cfg = default_config()
    judge = getattr(cfg, "auxiliary", None)
    judge = getattr(judge, "goal_judge", None) if judge is not None else None

    if judge is not None and judge.provider and judge.model:
        from opencomputer.plugins.registry import registry as plugin_registry

        provider_cls = plugin_registry.providers.get(judge.provider)
        if provider_cls is not None:
            provider = (
                provider_cls() if isinstance(provider_cls, type) else provider_cls
            )
            resp = await provider.complete(
                model=judge.model,
                messages=[{"role": "user", "content": prompt}],
                system=JUDGE_SYSTEM_PROMPT,
                max_tokens=120,
                temperature=0.0,
            )
            return (
                getattr(resp, "text", None)
                or getattr(resp, "content", None)
                or ""
            )
        logger.warning(
            "goal_judge.provider=%r not registered; falling back to chat provider",
            judge.provider,
        )

    from opencomputer.agent.aux_llm import complete_text

    return await complete_text(
        messages=[{"role": "user", "content": prompt}],
        system=JUDGE_SYSTEM_PROMPT,
        max_tokens=120,
        temperature=0.0,
    )


async def judge_goal(*, goal_text: str, last_response: str) -> JudgeVerdict:
    """Strict-JSON judge. Fails OPEN on any error (treated as not-done).

    Returns:
        :class:`JudgeVerdict` with parsed ``done`` + ``reason``. On parse
        error, network error, or empty response the verdict is
        ``done=False`` with a self-explaining ``reason`` — callers persist
        this on the goal row so ``/goal status`` shows what happened.
    """
    if not last_response:
        return JudgeVerdict(done=False, reason="(empty assistant response)")
    try:
        prompt = JUDGE_USER_TEMPLATE.format(
            goal_text=goal_text, last_response=last_response[:4000]
        )
        raw = await _call_judge_model(prompt)
    except Exception as exc:  # noqa: BLE001 — fail-open is the design
        logger.warning("goal judge call failed (failing open): %s", exc)
        return JudgeVerdict(
            done=False, reason=f"(judge error: {type(exc).__name__})"
        )
    if not raw or not raw.strip():
        return JudgeVerdict(done=False, reason="(empty judge response)")
    try:
        payload = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        return _unparseable()
    if not isinstance(payload, dict) or "done" not in payload:
        return _unparseable()
    done = bool(payload.get("done"))
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        reason = "(no reason given)"
    return JudgeVerdict(done=done, reason=reason)
