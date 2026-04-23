"""CoderIdentity — always-on preamble injection provider.

Unlike the other modes, this one has no gating flag. It fires every turn so
that the agent consistently frames itself as a careful coding agent even when
the user hasn't explicitly enabled plan or review mode.
"""

from __future__ import annotations

from modes import render  # type: ignore[import-not-found]
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext


class CoderIdentityInjectionProvider(DynamicInjectionProvider):
    #: Runs first, before any mode-gated injection.
    priority = 5

    @property
    def provider_id(self) -> str:
        return "coding-harness:coder-identity"

    def collect(self, ctx: InjectionContext) -> str | None:  # noqa: ARG002
        return render("coder_identity.j2")


__all__ = ["CoderIdentityInjectionProvider"]
