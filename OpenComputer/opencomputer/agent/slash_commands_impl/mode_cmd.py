"""``/mode <name>`` — show or set the permission mode for this session.

Plus shorthand commands:

  * ``/accept-edits`` — set ACCEPT_EDITS mode

``/auto`` and ``/plan`` (and ``/plan-off``) are defined elsewhere (auto_cmd
in core, plan in extensions/coding-harness) but follow the same writeback
pattern: mutate ``runtime.custom["permission_mode"]`` plus the legacy
session-key (``custom["plan_mode"]`` / ``custom["yolo_session"]``) so old
readers and the canonical helper agree.

State writes go through :func:`_set_mode` which clears all three legacy
keys before writing the chosen mode — prevents the situation where a user
goes ``/auto on`` then ``/plan`` and ends up with both ``yolo_session=True``
and ``plan_mode=True`` set.
"""

from __future__ import annotations

from plugin_sdk import PermissionMode, effective_permission_mode
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_VALID = ", ".join(m.value for m in PermissionMode)
_USAGE = f"Usage: /mode [{_VALID}]"


def _set_mode(runtime: RuntimeContext, mode: PermissionMode) -> None:
    """Set the canonical mode plus all legacy mirror keys, exclusively.

    Single source of truth for "switch this session's permission mode" —
    used by ``/mode``, ``/accept-edits``, the future Shift+Tab cycle, and
    any other surface that wants to override the active mode.
    """
    if mode == PermissionMode.DEFAULT:
        runtime.custom.pop("permission_mode", None)
        runtime.custom.pop("plan_mode", None)
        runtime.custom.pop("yolo_session", None)
        return
    runtime.custom["permission_mode"] = mode.value
    runtime.custom["plan_mode"] = mode == PermissionMode.PLAN
    runtime.custom["yolo_session"] = mode == PermissionMode.AUTO


class ModeCommand(SlashCommand):
    name = "mode"
    description = "Show or set the permission mode for this session"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        if not sub:
            current = effective_permission_mode(runtime).value
            return SlashCommandResult(
                output=f"Current mode: {current}\n{_USAGE}",
                handled=True,
            )
        try:
            mode = PermissionMode(sub)
        except ValueError:
            return SlashCommandResult(output=_USAGE, handled=True)
        _set_mode(runtime, mode)
        return SlashCommandResult(output=f"Mode set to {mode.value}.", handled=True)


class AcceptEditsCommand(SlashCommand):
    name = "accept-edits"
    description = (
        "Set mode to accept-edits (auto-approve Edit/Write/MultiEdit/NotebookEdit)"
    )

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        _set_mode(runtime, PermissionMode.ACCEPT_EDITS)
        return SlashCommandResult(
            output=(
                "Mode set to accept-edits. Edit/Write/MultiEdit/NotebookEdit will "
                "auto-approve; Bash and network calls still prompt."
            ),
            handled=True,
        )


__all__ = ["ModeCommand", "AcceptEditsCommand", "_set_mode"]
