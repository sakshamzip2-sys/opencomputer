"""Rewind tool — restore files to a previous checkpoint.

Either:
    Rewind(steps=N)          → rewind N checkpoints back from current
    Rewind(checkpoint_id=...)→ restore a specific checkpoint by id
    Rewind()                 → equivalent to Rewind(steps=1)
"""

from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class RewindTool(BaseTool):
    def __init__(self, ctx):
        self._ctx = ctx

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Rewind",
            description=(
                "Restore files to a previous checkpoint — undo a batch of edits the "
                "agent made. Pass `steps=N` to go back N checkpoints (default 1), "
                "`checkpoint_id` to jump to a specific one, or `list_checkpoints=true` "
                "to enumerate available checkpoints without restoring anything (cheap, "
                "use this first if unsure). CAUTION: this overwrites on-disk files "
                "with their checkpoint contents — anything the user changed manually "
                "since the checkpoint will be lost. Prefer CheckpointDiff first to "
                "review what's about to revert. Use Rewind for: a failed multi-step "
                "edit, a bad refactor you want to back out of, or an exploratory "
                "branch you no longer want."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Number of checkpoints to rewind.",
                    },
                    "checkpoint_id": {
                        "type": "string",
                        "description": "Specific checkpoint id to restore.",
                    },
                    "list_checkpoints": {
                        "type": "boolean",
                        "description": "If true, list available checkpoints and do nothing else.",
                    },
                },
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        params: dict[str, Any] = dict(call.arguments)

        checkpoints = self._ctx.rewind_store.list()
        if params.get("list_checkpoints"):
            if not checkpoints:
                return ToolResult(tool_call_id=call.id, content="(no checkpoints)")
            lines = [
                f"{i}. {cp.id} — {cp.label} — {cp.created_at}"
                for i, cp in enumerate(checkpoints, start=1)
            ]
            return ToolResult(tool_call_id=call.id, content="\n".join(lines))

        if not checkpoints:
            return ToolResult(
                tool_call_id=call.id,
                content="No checkpoints available.",
                is_error=True,
            )

        cp_id = params.get("checkpoint_id")
        if cp_id:
            target = next((c for c in checkpoints if c.id == cp_id), None)
            if target is None:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"No checkpoint {cp_id}",
                    is_error=True,
                )
        else:
            n = int(params.get("steps") or 1)
            if n > len(checkpoints):
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Only {len(checkpoints)} checkpoints exist.",
                    is_error=True,
                )
            target = checkpoints[n - 1]

        self._ctx.rewind_store.restore(target.id)
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"Restored checkpoint {target.id} ({target.label}). "
                f"{len(target.files)} file(s) rolled back."
            ),
        )


__all__ = ["RewindTool"]
