"""ReviewMode injection provider.

Fires when `runtime.custom["review_mode"]` is True. Tells the agent that a
reviewer subagent will inspect its edits. The actual spawn-on-edit is handled
by the companion `hooks/post_edit_review.py` (Phase 6d+).
"""

from __future__ import annotations

from modes import render  # type: ignore[import-not-found]
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext


class ReviewModeInjectionProvider(DynamicInjectionProvider):
    priority = 30

    @property
    def provider_id(self) -> str:
        return "coding-harness:review-mode"

    def collect(self, ctx: InjectionContext) -> str | None:
        if not ctx.runtime.custom.get("review_mode"):
            return None
        return render("review_mode.j2")


__all__ = ["ReviewModeInjectionProvider"]
