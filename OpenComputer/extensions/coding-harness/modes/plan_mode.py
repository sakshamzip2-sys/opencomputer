"""PlanMode injection provider — Jinja2-backed.

When `runtime.plan_mode` is True, injects the plan-mode directive block into
the system prompt this turn. Enforcement (hard-block on destructive tools) is
done separately by `hooks/plan_block.py`; this is the soft-guidance half.
"""

from __future__ import annotations

from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext

from hooks.plan_block import DESTRUCTIVE_TOOLS  # type: ignore[import-not-found]
from modes import render  # type: ignore[import-not-found]


class PlanModeInjectionProvider(DynamicInjectionProvider):
    priority = 10

    @property
    def provider_id(self) -> str:
        return "coding-harness:plan-mode"

    def collect(self, ctx: InjectionContext) -> str | None:
        if not ctx.runtime.plan_mode:
            return None
        return render("plan_mode.j2", blocked_tools=sorted(DESTRUCTIVE_TOOLS))


__all__ = ["PlanModeInjectionProvider"]
