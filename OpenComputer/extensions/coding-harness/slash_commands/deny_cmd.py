"""``/deny`` — reject the most-recent pending consent request.

Mirror of :mod:`approve_cmd` for the deny path. See its docstring for
the discovery + resolution flow; the only difference is ``decision=False``.

Resolution helper is duplicated rather than imported from ``approve_cmd``
because the production plugin loader synthesizes per-file unique module
names — sibling relative imports inside ``slash_commands/`` are not safe
to assume across plugin reloads.
"""

from __future__ import annotations

from typing import Any

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class DenyCommand(SlashCommand):
    name = "deny"
    description = "Deny the most-recent pending dangerous-command request"

    def __init__(self, harness_ctx: Any = None) -> None:
        self.harness_ctx = harness_ctx

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        return await _resolve_one(runtime, decision=False, label="denied")


# ---------------------------------------------------------------------------
# Local copy of the approve_cmd helper — see approve_cmd._resolve_one for the
# canonical version. Kept in sync by hand; both paths call gate.resolve_pending.
# ---------------------------------------------------------------------------


async def _resolve_one(
    runtime: RuntimeContext, *, decision: bool, label: str
) -> SlashCommandResult:
    custom = runtime.custom or {}
    gate = custom.get("consent_gate")
    if gate is None:
        return SlashCommandResult(
            output="no consent gate available (run inside the agent loop with consent enabled)",
            handled=True,
        )

    pending = _list_pending(gate)
    if not pending:
        return SlashCommandResult(output="no pending approvals", handled=True)

    session_id, capability_id = pending[-1]
    try:
        ok = gate.resolve_pending(
            session_id=session_id,
            capability_id=capability_id,
            decision=decision,
            persist=False,
        )
    except Exception as exc:  # noqa: BLE001
        return SlashCommandResult(
            output=f"resolve_pending failed: {exc}", handled=True,
        )
    if not ok:
        return SlashCommandResult(
            output="no pending approvals (already resolved)", handled=True,
        )
    return SlashCommandResult(
        output=f"{label}: {capability_id} (session {session_id})",
        handled=True,
    )


def _list_pending(gate: Any) -> list[tuple[str, str]]:
    list_fn = getattr(gate, "list_pending", None)
    if callable(list_fn):
        try:
            return list(list_fn())
        except Exception:  # noqa: BLE001
            return []

    pending = getattr(gate, "_pending_requests", None)
    if pending is None:
        return []
    try:
        return [
            (sid, cap)
            for (sid, cap), event in pending.items()
            if not event.is_set()
        ]
    except Exception:  # noqa: BLE001
        return []


__all__ = ["DenyCommand"]
