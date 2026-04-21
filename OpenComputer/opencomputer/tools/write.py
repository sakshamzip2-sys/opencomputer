"""Write tool — write content to a file, creating parent dirs."""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class WriteTool(BaseTool):
    parallel_safe = False  # writes to same path could race

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Write",
            description="Write content to a file. Overwrites the file if it exists. "
            "Creates parent directories as needed. file_path must be absolute.",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full contents to write.",
                    },
                },
                "required": ["file_path", "content"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        path = Path(args.get("file_path", ""))
        content = args.get("content", "")
        if not path.is_absolute():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: file_path must be absolute, got: {path}",
                is_error=True,
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error writing {path}: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"Wrote {len(content)} bytes to {path}",
        )
