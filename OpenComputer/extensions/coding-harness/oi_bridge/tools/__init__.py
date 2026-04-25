"""OI tools package — Tier 1 (introspection) only.

Tiers 2-5 were removed in OC's OI-trim cleanup (2026-04-25) because each
overlapped with a feature OpenComputer already provides:

* Tier 2 (email/SMS/Slack/Discord) → covered by channel adapters + MCP.
* Tier 3 (browser) → covered by ``extensions/opencli-scraper/``.
* Tier 4 (system control: run / kill process, system commands) → covered
  by the built-in ``BashTool``.
* Tier 5 (schedule task, custom code) → covered by ``opencomputer cron``
  (G.1) and ``BashTool``.

What remains is Tier 1: read files, list apps, clipboard, screenshot,
screen text, recent files, search, git log — features OI offers that
OpenComputer's core does NOT have natively. Re-exports the tier-1
ALL_TOOLS list so coding-harness/plugin.py can iterate:

    from extensions.coding_harness.oi_bridge.tools import ALL_TOOLS_BY_TIER
    for tier, tools in ALL_TOOLS_BY_TIER.items():
        for tool_cls in tools:
            api.register_tool(tool_cls(wrapper=wrapper, ...))
"""

from __future__ import annotations

from .tier_1_introspection import ALL_TOOLS as TIER_1_TOOLS

ALL_TOOLS_BY_TIER: dict[int, list] = {
    1: TIER_1_TOOLS,
}

ALL_TOOLS: list = [
    *TIER_1_TOOLS,
]

__all__ = ["ALL_TOOLS", "ALL_TOOLS_BY_TIER", "TIER_1_TOOLS"]
