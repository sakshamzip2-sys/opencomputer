"""post_edit_review — PostToolUse hook that queues review work in review mode.

When review-mode is active and a destructive tool just completed, this hook
appends a record to `session_state["pending_reviews"]`. A later runtime
integration (Phase 6f) consumes this queue via Delegate to spawn the reviewer
subagent with an isolated checkpoint store.

This hook is fire-and-forget (doesn't block the agent loop).
"""

from __future__ import annotations

from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

REVIEWABLE = frozenset({"Edit", "MultiEdit", "Write"})


def build_post_edit_review_hook_spec(*, harness_ctx) -> HookSpec:
    async def handler(ctx: HookContext) -> HookDecision | None:
        if ctx.runtime is None or not ctx.runtime.custom.get("review_mode"):
            return None
        if ctx.tool_call is None or ctx.tool_call.name not in REVIEWABLE:
            return None
        pending = harness_ctx.session_state.get("pending_reviews", []) or []
        pending.append(
            {
                "tool": ctx.tool_call.name,
                "path": ctx.tool_call.arguments.get("path")
                or ctx.tool_call.arguments.get("file"),
                "tool_call_id": ctx.tool_call.id,
            }
        )
        harness_ctx.session_state.set("pending_reviews", pending)
        return None

    return HookSpec(
        event=HookEvent.POST_TOOL_USE,
        handler=handler,
        matcher=None,
        fire_and_forget=True,
    )


__all__ = ["build_post_edit_review_hook_spec"]
