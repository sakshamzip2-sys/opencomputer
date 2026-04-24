"""ReviewMode injection provider.

Fires when `runtime.custom["review_mode"]` is True. Tells the agent that a
reviewer subagent will inspect its edits. The actual spawn-on-edit is handled
by the companion `hooks/post_edit_review.py` (Phase 6d+).

IV.2 (turn-counting throttle): the FULL reminder fires on turn 1 and every
5th turn after (1, 6, 11, 16, ...). Intervening turns get a SPARSE one-liner.
``turn_index == 0`` (neutral default) maps to FULL so legacy callers that
never thread a counter keep seeing the guidance.
"""

from __future__ import annotations

from modes import render  # type: ignore[import-not-found]
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext

#: Every Nth turn after the first is a FULL reminder. Kept in sync with
#: ``plan_mode.py:_FULL_EVERY_N`` — both throttle on the same cadence so
#: the user's mental model is one rule, not per-mode trivia.
_FULL_EVERY_N = 5

_SPARSE_REMINDER = (
    "Review mode active. A reviewer will inspect your edits — write "
    "review-surviving code.\n"
)


def _is_full_turn(turn_index: int) -> bool:
    """Same rule as ``plan_mode._is_full_turn``. Duplicated here (rather
    than imported) so the two providers stay independently deployable —
    a future SDK refactor might land them in separate packages."""
    if turn_index <= 0:
        return True
    return turn_index % _FULL_EVERY_N == 1


class ReviewModeInjectionProvider(DynamicInjectionProvider):
    priority = 30

    @property
    def provider_id(self) -> str:
        return "coding-harness:review-mode"

    async def collect(self, ctx: InjectionContext) -> str | None:
        if not ctx.runtime.custom.get("review_mode"):
            return None
        if _is_full_turn(ctx.turn_index):
            return render("review_mode.j2")
        return _SPARSE_REMINDER


__all__ = ["ReviewModeInjectionProvider"]
