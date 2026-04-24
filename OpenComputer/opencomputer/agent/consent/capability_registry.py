"""CapabilityRegistry — plugin loader registers tool → CapabilityClaims here.

Populated at plugin-activation time by `opencomputer.plugins.loader` from
each tool class's `capability_claims` attribute. `ConsentGate` consults this
at runtime to know what claims apply to an incoming tool call.
"""
from __future__ import annotations

from plugin_sdk import CapabilityClaim


class CapabilityRegistry:
    def __init__(self) -> None:
        self._by_tool: dict[str, list[CapabilityClaim]] = {}

    def register(
        self,
        plugin_id: str,
        tool_name: str,
        claims: list[CapabilityClaim],
    ) -> None:
        existing = self._by_tool.setdefault(tool_name, [])
        for c in claims:
            if c not in existing:
                existing.append(c)

    def claims_for_tool(self, tool_name: str) -> list[CapabilityClaim]:
        return list(self._by_tool.get(tool_name, []))
