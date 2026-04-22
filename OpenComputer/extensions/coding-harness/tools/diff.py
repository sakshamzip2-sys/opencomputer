"""Diff tool — unified diff between current files and the most recent checkpoint.

Useful after an Edit/MultiEdit series when the user (or reviewer) wants to
see what actually changed vs. the last known-good state.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class DiffTool(BaseTool):
    def __init__(self, ctx):
        self._ctx = ctx

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Diff",
            description=(
                "Show a unified diff between the most recent checkpoint and "
                "current on-disk files. Pass `checkpoint_id` to diff against "
                "a specific checkpoint instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "checkpoint_id": {
                        "type": "string",
                        "description": "Specific checkpoint id to diff against.",
                    },
                    "context_lines": {
                        "type": "integer",
                        "default": 3,
                        "description": "Lines of context around each hunk.",
                    },
                },
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = dict(call.arguments)
        context_lines = int(args.get("context_lines") or 3)
        cp_id = args.get("checkpoint_id")

        checkpoints = self._ctx.rewind_store.list()
        if not checkpoints:
            return ToolResult(
                tool_call_id=call.id,
                content="No checkpoints to diff against.",
                is_error=True,
            )

        target = None
        if cp_id:
            target = next((c for c in checkpoints if c.id == cp_id), None)
            if target is None:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"No checkpoint {cp_id}",
                    is_error=True,
                )
        else:
            target = checkpoints[0]

        diff_lines: list[str] = []
        for rel_path, old_bytes in target.files.items():
            live = self._ctx.rewind_store.workspace_root / rel_path
            new_bytes = b""
            if live.exists() and live.is_file():
                new_bytes = live.read_bytes()

            old_text = _decode(old_bytes)
            new_text = _decode(new_bytes)
            if old_text == new_text:
                continue
            diff_lines.extend(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=f"a/{rel_path}@{target.id}",
                    tofile=f"b/{rel_path}",
                    n=context_lines,
                )
            )

        if not diff_lines:
            return ToolResult(
                tool_call_id=call.id,
                content=f"No changes since checkpoint {target.id}.",
            )
        return ToolResult(tool_call_id=call.id, content="".join(diff_lines))


def _decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


__all__ = ["DiffTool"]
