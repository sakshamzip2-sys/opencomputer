"""PlanMode injection provider — Jinja2-backed with turn-counting throttle.

When `runtime.plan_mode` is True, injects the plan-mode directive block into
the system prompt this turn. Enforcement (hard-block on destructive tools) is
done separately by `hooks/plan_block.py`; this is the soft-guidance half.

IV.2 (turn-counting throttle): the FULL reminder fires on turn 1 and every
5th turn after (1, 6, 11, 16, ...). Intervening turns get a SPARSE one-liner.
Saves ~500 tokens/turn after ~50 turns in long plan sessions. Mirrors Kimi
CLI (sources/kimi-cli/src/kimi_cli/soul/dynamic_injections/plan_mode.py:27-29).
``turn_index == 0`` (the neutral "unthreaded" default) is treated as the
first exposure and returns the FULL reminder, so legacy callers that never
pass a counter don't silently get sparse content forever.
"""

from __future__ import annotations

from hooks.plan_block import DESTRUCTIVE_TOOLS  # type: ignore[import-not-found]

from modes import render  # type: ignore[import-not-found]
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext

#: Every Nth turn after the first is a FULL reminder. 5 matches Kimi CLI.
_FULL_EVERY_N = 5

#: One-line nudge used on the ``N-1`` turns between full reminders. Keeps
#: plan-mode top-of-mind without re-serializing the whole prompt every turn.
_SPARSE_REMINDER = (
    "Plan mode active. Describe the plan; no destructive tools; user will "
    "approve before execution.\n"
)


def _is_full_turn(turn_index: int) -> bool:
    """Return ``True`` when this turn should emit the FULL reminder.

    FULL fires on turn 1 and every 5th turn thereafter (1, 6, 11, ...).
    ``turn_index == 0`` is the neutral default — treated as the first
    exposure so unthreaded callers still see the full guidance.
    """
    if turn_index <= 0:
        return True
    return turn_index % _FULL_EVERY_N == 1


class PlanModeInjectionProvider(DynamicInjectionProvider):
    priority = 10

    @property
    def provider_id(self) -> str:
        return "coding-harness:plan-mode"

    async def collect(self, ctx: InjectionContext) -> str | None:
        from plugin_sdk import PermissionMode, effective_permission_mode

        if effective_permission_mode(ctx.runtime) != PermissionMode.PLAN:
            return None
        if _is_full_turn(ctx.turn_index):
            return render("plan_mode.j2", blocked_tools=sorted(DESTRUCTIVE_TOOLS))
        return _SPARSE_REMINDER


__all__ = ["PlanModeInjectionProvider"]
