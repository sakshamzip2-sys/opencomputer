"""Backwards-compat shim — the plan-mode provider and block-hook moved.

Post Phase 6d:
    Provider → `modes/plan_mode.py`
    Hook     → `hooks/plan_block.py`

Re-exported from here so existing tests (and any external imports) keep
working without change. New code should import from the canonical locations.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Ensure this plugin's root is on sys.path so nested imports (hooks.*, modes.*)
# resolve even when the module is loaded directly (not via the plugin loader).
_PLUGIN_ROOT = _Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_PLUGIN_ROOT))

from hooks.plan_block import (  # type: ignore[import-not-found]  # noqa: E402
    DESTRUCTIVE_TOOLS,
    build_plan_mode_hook_spec,
    plan_mode_block_hook,
)
from modes.plan_mode import (  # noqa: E402
    PlanModeInjectionProvider,  # type: ignore[import-not-found]
)

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
