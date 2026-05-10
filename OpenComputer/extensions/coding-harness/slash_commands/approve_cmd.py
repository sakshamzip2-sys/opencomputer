"""``/approve`` — approve the most-recent pending consent request.

Bridges the slash-command surface to :class:`ConsentGate.resolve_pending`.
The gate is reached via ``runtime.custom['consent_gate']`` (set by the
agent loop / gateway dispatch when a gate is wired). With no pending
requests we return a friendly note.

Pending discovery:
  * If the gate exposes ``list_pending()``, that is the source of truth.
  * Otherwise we fall back to introspecting the gate's internal
    ``_pending_requests`` dict (the public API used by Telegram /
    Slack / Matrix adapters).

The most-recent pending entry is resolved. Out-of-band callbacks (button
clicks, channel-side approval) still work — this is just a manual escape
hatch from the chat surface.
"""

from __future__ import annotations

from typing import Any

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class ApproveCommand(SlashCommand):
    name = "approve"
    description = "Approve the most-recent pending dangerous-command request"

    def __init__(self, harness_ctx: Any = None) -> None:
        # Accept harness_ctx to match the plugin's SlashCommand contract,
        # but we don't use it — consent is core, not harness-specific.
        self.harness_ctx = harness_ctx

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        return await _resolve_one(runtime, decision=True, label="approved")


__all__ = ["ApproveCommand"]


# ---------------------------------------------------------------------------
# Shared resolution helper used by /approve and /deny.
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
        return SlashCommandResult(
            output="no pending approvals",
            handled=True,
        )

    session_id, capability_id = pending[-1]  # most recent
    try:
        ok = gate.resolve_pending(
            session_id=session_id,
            capability_id=capability_id,
            decision=decision,
            persist=False,
        )
    except Exception as exc:  # noqa: BLE001 — surface the error to user
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
    """Return all pending ``(session_id, capability_id)`` entries.

    Prefer a ``list_pending()`` method when the gate provides one; fall
    back to peeking at the ``_pending_requests`` dict that the real
    ``ConsentGate`` maintains internally. Returning ``[]`` on any error
    keeps the slash command robust against future gate refactors.
    """
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
