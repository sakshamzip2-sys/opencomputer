"""Minimal SlashCommand contract.

Mirrors the planned `plugin_sdk.SlashCommand` shape so the harness commands
work today via duck-typing and can trivially upgrade when the SDK ships the
formal base class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SlashCommand(ABC):
    name: str
    description: str

    @abstractmethod
    async def execute(self, args: str, runtime, harness_ctx) -> str:
        """Run the command. `args` is the raw text after the command name.

        `runtime` is the per-invocation RuntimeContext (may be mutable via
        `runtime.custom`). `harness_ctx` is the shared HarnessContext so
        commands can touch the rewind store, session state, etc.
        """
        ...


__all__ = ["SlashCommand"]
