"""Read tool — read the contents of a file."""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

from opencomputer.tools._file_read_state import mark_read


class ReadTool(BaseTool):
    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Read",
            description=(
                "Read a file from disk. Output is prefixed with line numbers so you can "
                "reference exact lines back to the user. Use this for any path you need "
                "to inspect — config, code, logs. Prefer Read over Bash 'cat'/'head'/"
                "'tail': line-numbered output is LLM-friendly and the harness tracks "
                "file state. Don't re-Read a file you just edited — Edit/Write would "
                "have errored if the change failed. For large files, pass `offset`+`limit` "
                "to slice instead of reading the whole thing. file_path must be absolute."
            ),
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

        # Record that this path has been Read so Edit/MultiEdit can
        # honour their "Read first" contract.
        mark_read(path)

        lines = text.splitlines()
        offset = max(1, int(args.get("offset", 1)))
        limit = int(args.get("limit", 2000))
        start_idx = offset - 1
        end_idx = start_idx + limit
        slice_ = lines[start_idx:end_idx]
        numbered = "\n".join(f"{start_idx + i + 1:>6}\t{ln}" for i, ln in enumerate(slice_))
        return ToolResult(tool_call_id=call.id, content=numbered)
