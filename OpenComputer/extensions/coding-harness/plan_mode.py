"""Backwards-compat shim — the plan-mode provider and block-hook moved.

Post Phase 6d:
    Provider → `modes/plan_mode.py`
    Hook     → `hooks/plan_block.py`

Re-exported from here so existing tests (and any external imports) keep
working without change. New code should import from the canonical locations.
"""

from __future__ import annotations

from hooks.plan_block import (  # type: ignore[import-not-found]
    DESTRUCTIVE_TOOLS,
    build_plan_mode_hook_spec,
    plan_mode_block_hook,
)
from modes.plan_mode import PlanModeInjectionProvider  # type: ignore[import-not-found]

PLAN_MODE_TEXT = (
    "## PLAN MODE ACTIVE\n\n"
    "(text now comes from prompts/plan_mode.j2 — this constant retained only "
    "for code that might still reference it; prefer reading the template.)"
)

__all__ = [
    "PlanModeInjectionProvider",
    "plan_mode_block_hook",
    "build_plan_mode_hook_spec",
    "DESTRUCTIVE_TOOLS",
    "PLAN_MODE_TEXT",
]
