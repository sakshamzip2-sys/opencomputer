"""AcceptEditsMode injection provider.

Fires when ``effective_permission_mode(runtime) == ACCEPT_EDITS``.
Communicates to the agent that small edits will be auto-accepted and the
user will use ``/undo`` to revert unwanted ones.

PR-3 (2026-04-29) — switched from reading the legacy
``runtime.custom["accept_edits"]`` boolean to the canonical helper, which
also picks up ``--accept-edits`` CLI flag, ``/mode accept-edits``,
``/accept-edits``, and Shift+Tab cycling uniformly.
"""

from __future__ import annotations

from modes import render  # type: ignore[import-not-found]
from plugin_sdk import PermissionMode, effective_permission_mode
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext


class AcceptEditsModeInjectionProvider(DynamicInjectionProvider):
    priority = 20

    @property
    def provider_id(self) -> str:
        return "coding-harness:accept-edits-mode"

    async def collect(self, ctx: InjectionContext) -> str | None:
        if effective_permission_mode(ctx.runtime) != PermissionMode.ACCEPT_EDITS:
            return None
        return render("accept_edits_mode.j2")


__all__ = ["AcceptEditsModeInjectionProvider"]
