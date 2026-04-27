"""MultiEdit tool — atomic batch edits to a single file."""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class MultiEditTool(BaseTool):
    parallel_safe = False

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="MultiEdit",
            description=(
                "Apply multiple Edit operations to a SINGLE file atomically. All edits "
                "are applied in-memory in order; the file is written exactly once at "
                "the end. If ANY edit fails (missing text, non-unique without "
                "replace_all, no-op pair), the whole batch is rolled back and the file "
                "stays unchanged. Use this when you need to make 3+ changes to the same "
                "file — it's more efficient than calling Edit repeatedly and gives you "
                "all-or-nothing semantics. CAUTION: edits run sequentially, so a later "
                "edit's `old_string` must match what earlier edits leave behind. Read "
                "first (same harness rule as Edit). For NEW files use Write; for changes "
                "across multiple files, dispatch one MultiEdit per file (preferably in "
                "parallel where independent). Prefer MultiEdit + review over repeatedly "
                "Writing the same file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to edit.",
                    },
                    "edits": {
                        "type": "array",
                        "description": "List of edits to apply in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                                "replace_all": {"type": "boolean"},
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["file_path", "edits"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        path = Path(args.get("file_path", ""))
        edits = args.get("edits", [])

        if not path.is_absolute():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: file_path must be absolute, got: {path}",
                is_error=True,
            )
        if not path.exists() or not path.is_file():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: file does not exist: {path}",
                is_error=True,
            )
        if not isinstance(edits, list) or not edits:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: edits must be a non-empty list",
                is_error=True,
            )

        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error reading {path}: {type(e).__name__}: {e}",
                is_error=True,
            )

        original = text
        total_replacements = 0

        for i, edit in enumerate(edits):
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            replace_all = bool(edit.get("replace_all", False))

            if old == new:
                # Rollback: return without writing.
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Error: edit {i}: old_string == new_string (no change). Rolled back.",
                    is_error=True,
                )

            count = text.count(old)
            if count == 0:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Error: edit {i}: old_string not found. Rolled back {total_replacements} prior edits.",
                    is_error=True,
                )
            if count > 1 and not replace_all:
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        f"Error: edit {i}: old_string appears {count} times — "
                        f"expand with more context or set replace_all=true. "
                        f"Rolled back {total_replacements} prior edits."
                    ),
                    is_error=True,
                )

            text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            total_replacements += count if replace_all else 1

        # All edits succeeded in memory. Write once.
        if text == original:
            # Possible for a no-op batch (e.g. old and new match after prior edits)
            return ToolResult(
                tool_call_id=call.id, content="All edits produced no net change"
            )
        try:
            path.write_text(text, encoding="utf-8")
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error writing {path}: {type(e).__name__}: {e}",
                is_error=True,
            )

        return ToolResult(
            tool_call_id=call.id,
            content=f"Applied {len(edits)} edit(s) to {path} ({total_replacements} replacements)",
        )


__all__ = ["MultiEditTool"]
