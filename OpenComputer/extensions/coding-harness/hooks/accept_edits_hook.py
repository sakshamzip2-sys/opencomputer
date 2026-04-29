"""accept_edits_hook — PreToolUse auto-approver for the Edit-family tools.

Fires only when ``effective_permission_mode(runtime) == ACCEPT_EDITS``.
Returns a ``HookDecision(decision="approve")`` for tool names in
:data:`AUTO_APPROVED_TOOLS` so the F1 ConsentGate skips the per-action
prompt for those tools. Bash and network tools fall through (no decision
returned), preserving their normal consent flow even in accept-edits mode.

Design rationale: the user opted in to unprompted *edits*, not unprompted
*shell*. Bash that happens to mutate files (``sed -i``, ``> path``) is
deliberately NOT included. Adding a tool to ``AUTO_APPROVED_TOOLS`` is an
explicit opt-in; new file-edit tools must be listed here individually.
"""
from __future__ import annotations

from plugin_sdk import PermissionMode, effective_permission_mode
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

#: Exact tool-name allowlist. Opt-in, not pattern-matched.
AUTO_APPROVED_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


async def accept_edits_hook(ctx: HookContext) -> HookDecision | None:
    if ctx.runtime is None:
        return None
    if effective_permission_mode(ctx.runtime) != PermissionMode.ACCEPT_EDITS:
        return None
    if ctx.tool_call is None:
        return None
    if ctx.tool_call.name not in AUTO_APPROVED_TOOLS:
        return None
    return HookDecision(
        decision="approve",
        reason=f"accept-edits mode auto-approved {ctx.tool_call.name}",
    )


def build_accept_edits_hook_spec() -> HookSpec:
    return HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        handler=accept_edits_hook,
        matcher=None,
        fire_and_forget=False,
    )


__all__ = [
    "AUTO_APPROVED_TOOLS",
    "accept_edits_hook",
    "build_accept_edits_hook_spec",
]
