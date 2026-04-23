"""scope_check_hook — PreToolUse hook that enforces default permission scopes.

Runs BEFORE auto_checkpoint (same event), and blocks the tool call entirely
when the target path or command is outside the allowed scope.
"""

from __future__ import annotations

from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

from .scope_check import SCOPED_BASH_TOOLS, SCOPED_FILE_TOOLS, is_allowed


def _value_to_check(tool_call) -> str | None:
    if tool_call.name in SCOPED_FILE_TOOLS:
        args = tool_call.arguments
        for key in ("path", "file", "file_path", "target_file"):
            v = args.get(key)
            if isinstance(v, str):
                return v
        return None
    if tool_call.name in SCOPED_BASH_TOOLS:
        v = tool_call.arguments.get("command")
        if isinstance(v, str):
            return v
        return None
    return None


async def scope_check_hook(ctx: HookContext) -> HookDecision | None:
    if ctx.tool_call is None:
        return None
    value = _value_to_check(ctx.tool_call)
    if value is None:
        return None
    allowed, reason = is_allowed(ctx.tool_call.name, value)
    if allowed:
        return None
    return HookDecision(
        decision="block",
        reason=f"coding-harness scope check refused: {reason}",
    )


def build_scope_check_hook_spec() -> HookSpec:
    return HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        handler=scope_check_hook,
        matcher=None,
        fire_and_forget=False,
    )


__all__ = ["scope_check_hook", "build_scope_check_hook_spec"]
