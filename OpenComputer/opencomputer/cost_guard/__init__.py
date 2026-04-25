"""OpenComputer cost guard — per-provider API budget tracking + caps.

Guards against runaway API spend (cron job loops, voice in tight feedback,
agent stuck retrying). Records every paid API call with its cost, enforces
daily + monthly caps per provider, and blocks new calls when caps are hit.

Storage: ``<profile_home>/cost_guard.json`` (atomic writes, mode 0600).

Public surface:

- :class:`CostGuard` — singleton-friendly API: ``record_usage`` /
  ``check_budget`` / ``current_usage`` / ``set_limit``.
- :func:`get_default_guard` — module-level lazy instance using
  ``_home() / "cost_guard.json"``.

Integration pattern (called from provider plugins or agent loop)::

    from opencomputer.cost_guard import get_default_guard, BudgetExceeded

    guard = get_default_guard()
    cost = estimated_cost_usd(operation)
    decision = guard.check_budget("openai", projected_cost_usd=cost)
    if not decision.allowed:
        raise BudgetExceeded(decision.reason)
    # ... make the call ...
    guard.record_usage("openai", cost_usd=actual_cost)

The CLI surface (``opencomputer/cli_cost.py`` exposed as
``opencomputer cost {show,set-limit,reset}``) lets users inspect + tune
limits without writing the JSON file by hand.
"""

from __future__ import annotations

from opencomputer.cost_guard.guard import (
    BudgetDecision,
    BudgetExceeded,
    CostGuard,
    ProviderUsage,
    get_default_guard,
)

__all__ = [
    "BudgetDecision",
    "BudgetExceeded",
    "CostGuard",
    "ProviderUsage",
    "get_default_guard",
]
