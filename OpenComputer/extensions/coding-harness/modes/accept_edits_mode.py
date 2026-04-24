"""AcceptEditsMode injection provider.

Fires when `runtime.custom["accept_edits"]` is True. Communicates to the agent
that small edits will be auto-accepted and the user will use /undo to revert
unwanted ones.
"""

from __future__ import annotations

from modes import render  # type: ignore[import-not-found]
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext


class AcceptEditsModeInjectionProvider(DynamicInjectionProvider):
    priority = 20

    @property
    def provider_id(self) -> str:
        return "coding-harness:accept-edits-mode"

    async def collect(self, ctx: InjectionContext) -> str | None:
        if not ctx.runtime.custom.get("accept_edits"):
            return None
        return render("accept_edits_mode.j2")


__all__ = ["AcceptEditsModeInjectionProvider"]
