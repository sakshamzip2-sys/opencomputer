"""plan_block — PreToolUse hook that refuses destructive tools while in plan mode.

Two layers of defence:
    1. Name-based block (original behaviour): any tool call whose ``name`` is in
       :data:`DESTRUCTIVE_TOOLS` is refused while ``runtime.plan_mode`` is True.
    2. Bash-command heuristic (II.4): when the tool is ``Bash``, the ``command``
       argument is additionally scanned by
       :func:`opencomputer.tools.bash_safety.detect_destructive_with_context`.
       That wrapper consults BOTH ``security.command_allowlist`` AND
       ``sandbox.strategy`` from the active profile's config — a user-allowlisted
       command or a container-isolated sandbox short-circuits the heuristic.
       Hardline patterns (:mod:`opencomputer.security.hardline`) STILL apply via
       the Bash tool's own entry-point check; this hook only handles the
       advisory tier.

The heuristic fires only when plan_mode is active. Outside plan_mode, Bash is
the user's power-tool and we don't second-guess the command shape.
"""

from __future__ import annotations

from opencomputer.tools.bash_safety import detect_destructive_with_context
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

DESTRUCTIVE_TOOLS = frozenset(
    {
        "Edit",
        "Write",
        "Bash",
        "MultiEdit",
        "StartProcess",
        "KillProcess",
    }
)


async def plan_mode_block_hook(ctx: HookContext) -> HookDecision | None:
    from plugin_sdk import PermissionMode, effective_permission_mode

    if ctx.runtime is None:
        return None
    if effective_permission_mode(ctx.runtime) != PermissionMode.PLAN:
        return None
    if ctx.tool_call is None:
        return None

    # II.4: when the call is Bash, prefer the specific destructive-pattern
    # reason over the generic "Bash refused" string. This gives the model
    # something concrete to react to ("ah, I was trying to git reset --hard
    # — let me describe it instead").
    if ctx.tool_call.name == "Bash":
        command = ctx.tool_call.arguments.get("command", "") if ctx.tool_call.arguments else ""
        match = detect_destructive_with_context(command)
        if match is not None:
            return HookDecision(
                decision="block",
                reason=(
                    f"plan mode — destructive pattern detected: {match.reason}. "
                    f"Describe the plan instead; the user will confirm before execution."
                ),
            )
        # Fall through to the name-based block below — Bash is still
        # in DESTRUCTIVE_TOOLS and gets refused regardless of command shape
        # while plan_mode is active.

    if ctx.tool_call.name not in DESTRUCTIVE_TOOLS:
        return None
    return HookDecision(
        decision="block",
        reason=(
            f"plan mode active — {ctx.tool_call.name} refused. "
            f"Describe the plan instead; the user will confirm before execution."
        ),
    )


def build_plan_mode_hook_spec() -> HookSpec:
    return HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        handler=plan_mode_block_hook,
        matcher=None,
        fire_and_forget=False,
    )


__all__ = [
    "DESTRUCTIVE_TOOLS",
    "plan_mode_block_hook",
    "build_plan_mode_hook_spec",
]
