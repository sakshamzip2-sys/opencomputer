# ruff: noqa: N999  -- directory 'oi-capability' has a hyphen (required by plugin manifest)
"""OI tools package — 23 tools across 5 risk tiers.

Re-exports ALL_TOOLS lists from each tier module so Phase 5 can iterate:

    from extensions.oi_capability.tools import ALL_TOOLS_BY_TIER
    for tier, tools in ALL_TOOLS_BY_TIER.items():
        for tool_cls in tools:
            api.register_tool(tool_cls(wrapper=wrapper, ...))
"""

from __future__ import annotations

from .tier_1_introspection import ALL_TOOLS as TIER_1_TOOLS
from .tier_2_communication import ALL_TOOLS as TIER_2_TOOLS
from .tier_3_browser import ALL_TOOLS as TIER_3_TOOLS
from .tier_4_system_control import ALL_TOOLS as TIER_4_TOOLS
from .tier_5_advanced import ALL_TOOLS as TIER_5_TOOLS

ALL_TOOLS_BY_TIER: dict[int, list] = {
    1: TIER_1_TOOLS,
    2: TIER_2_TOOLS,
    3: TIER_3_TOOLS,
    4: TIER_4_TOOLS,
    5: TIER_5_TOOLS,
}

ALL_TOOLS: list = [
    *TIER_1_TOOLS,
    *TIER_2_TOOLS,
    *TIER_3_TOOLS,
    *TIER_4_TOOLS,
    *TIER_5_TOOLS,
]

__all__ = ["ALL_TOOLS", "ALL_TOOLS_BY_TIER", "TIER_1_TOOLS", "TIER_2_TOOLS",
           "TIER_3_TOOLS", "TIER_4_TOOLS", "TIER_5_TOOLS"]
