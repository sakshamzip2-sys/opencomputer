"""Read tool — read the contents of a file."""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class ReadTool(BaseTool):
    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Read",
            description="Read the contents of a file from disk. Returns the text, "
            "prefixed with line numbers. Supports optional offset and limit "
            "for reading slices of large files.",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to read.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-indexed).",
                        "minimum": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read.",
                        "minimum": 1,
                    },
                },
                "required": ["file_path"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        path = Path(args.get("file_path", ""))
        if not path.is_absolute():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: file_path must be absolute, got: {path}",
                is_error=True,
            )
        if not path.exists():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: file does not exist: {path}",
                is_error=True,
            )
        if not path.is_file():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: path is not a file: {path}",
                is_error=True,
            )

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error reading {path}: {type(e).__name__}: {e}",
                is_error=True,
            )

        lines = text.splitlines()
        offset = max(1, int(args.get("offset", 1)))
        limit = int(args.get("limit", 2000))
        start_idx = offset - 1
        end_idx = start_idx + limit
        slice_ = lines[start_idx:end_idx]
        numbered = "\n".join(f"{start_idx + i + 1:>6}\t{ln}" for i, ln in enumerate(slice_))
        return ToolResult(tool_call_id=call.id, content=numbered)
