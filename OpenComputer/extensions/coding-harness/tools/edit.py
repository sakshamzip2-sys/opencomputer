"""Edit tool — surgical find/replace with uniqueness check (Claude Code shape)."""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class EditTool(BaseTool):
    parallel_safe = False  # writes to disk

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Edit",
            description=(
                "Surgical find-and-replace in an existing file. The preferred way to "
                "modify code: cheaper than Write (only sends the diff), preserves "
                "everything you don't touch, and uniqueness check catches accidental "
                "double-replacements.\n\n"
                "- READ FIRST: you must Read the file at least once in this conversation "
                "before editing — the harness tracks state and will refuse otherwise. "
                "When matching against Read's output, strip the line-number prefix "
                "(format is `<num>\\t<content>`); never include the line numbers in "
                "old_string.\n"
                "- `old_string` must match the file content EXACTLY, including all "
                "whitespace and indentation.\n"
                "- `old_string` must be UNIQUE in the file. If it appears multiple times "
                "the tool errors — expand old_string with more surrounding context until "
                "unique, or pass `replace_all: true`.\n"
                "- Don't use Edit for new files; use Write. Don't use it on .ipynb "
                "notebooks; use NotebookEdit.\n"
                "- Prefer Edit over Bash sed/awk: structured errors and the harness can "
                "track the change for rewind/diff."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to edit.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact text to replace (must be unique unless replace_all).",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The replacement text.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences (default false).",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        path = Path(args.get("file_path", ""))
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))

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
        if old == new:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: old_string and new_string are identical — no change",
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

        count = text.count(old)
        if count == 0:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: old_string not found in {path}. "
                    f"Verify the exact text including whitespace."
                ),
                is_error=True,
            )
        if count > 1 and not replace_all:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: old_string appears {count} times in {path}. "
                    f"Expand old_string with more context (surrounding lines) until it is "
                    f"unique, or pass replace_all=true."
                ),
                is_error=True,
            )

        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        try:
            path.write_text(new_text, encoding="utf-8")
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error writing {path}: {type(e).__name__}: {e}",
                is_error=True,
            )

        n = count if replace_all else 1
        return ToolResult(
            tool_call_id=call.id,
            content=f"Edited {path} ({n} replacement{'s' if n != 1 else ''})",
        )


__all__ = ["EditTool"]
