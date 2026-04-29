"""``/mode <name>`` — show or set the permission mode for this session.

``/auto``, ``/accept-edits``, and ``/plan`` (plus ``/plan-off``) are defined
elsewhere (auto_cmd in core; ``/accept-edits``/``/plan`` in
extensions/coding-harness) but follow the same writeback pattern via
:func:`_set_mode` — mutate ``runtime.custom["permission_mode"]`` plus the
legacy session key (``custom["plan_mode"]`` / ``custom["yolo_session"]`` /
``custom["accept_edits"]``) so old readers and the canonical helper agree.

State writes go through :func:`_set_mode` which clears all legacy mirror
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
    used by ``/mode``, ``/accept-edits`` (extension), ``/auto``, ``/plan``,
    the Shift+Tab cycle in the TUI, and any other surface that wants to
    override the active mode.
    """
    if mode == PermissionMode.DEFAULT:
        runtime.custom.pop("permission_mode", None)
        runtime.custom.pop("plan_mode", None)
        runtime.custom.pop("yolo_session", None)
        runtime.custom.pop("accept_edits", None)
        return
    runtime.custom["permission_mode"] = mode.value
    runtime.custom["plan_mode"] = mode == PermissionMode.PLAN
    runtime.custom["yolo_session"] = mode == PermissionMode.AUTO
    runtime.custom["accept_edits"] = mode == PermissionMode.ACCEPT_EDITS


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


__all__ = ["ModeCommand", "_set_mode"]
