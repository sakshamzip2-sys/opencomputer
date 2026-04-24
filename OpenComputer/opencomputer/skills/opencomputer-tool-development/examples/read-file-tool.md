# Example — a simple read-only tool

A minimal file reader. Parallel-safe (pure read, no state mutation) and
suitable as a template for any tool that never writes.

```python
"""LineCount — count non-empty lines in a text file."""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class LineCountTool(BaseTool):
    #: Pure read, no shared state — safe to run alongside other
    #: read-only tools in the same batch.
    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="LineCount",
            description=(
                "Count the number of non-empty lines in a text file. "
                "Skips lines that are blank or whitespace-only. Use this "
                "when you need a quick size estimate without reading the body."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Absolute filesystem path to the file to count. "
                            "Must exist and be UTF-8-decodable."
                        ),
                    }
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        path_str = call.arguments.get("file_path")
        if not isinstance(path_str, str) or not path_str:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: 'file_path' is required and must be a non-empty string.",
                is_error=True,
            )

        path = Path(path_str)
        if not path.is_absolute():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {path_str!r} is not absolute.",
                is_error=True,
            )
        if not path.exists():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: no file at {path_str!r}.",
                is_error=True,
            )
        if not path.is_file():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {path_str!r} is not a regular file.",
                is_error=True,
            )

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {path_str!r} is not UTF-8: {e}.",
                is_error=True,
            )

        count = sum(1 for line in text.splitlines() if line.strip())
        return ToolResult(
            tool_call_id=call.id,
            content=f"{count} non-empty line(s) in {path.name}",
        )


def register(api) -> None:
    api.register_tool(LineCountTool())
```

## Why this is parallel-safe

- No shared state between invocations.
- No filesystem writes.
- Deterministic output for given input.
- No external processes or network calls.

Two `LineCount` calls on different files can absolutely run in parallel;
two on the SAME file can too (both just read). The agent loop's
`_all_parallel_safe` check at `opencomputer/agent/loop.py:913` will
approve a batch containing multiple `LineCount` calls — no hardcoded
rejection, no path-scope conflict.

## Things this example deliberately does not do

- **No workspace check.** A production read tool would refuse paths
  outside the workspace root. That logic usually belongs in a
  `PreToolUse` hook (see the hook-authoring skill), not in the tool.
- **No caching.** Filesystem I/O is fast enough that caching read
  results inside a tool adds bug surface. Let the OS page cache do it.
- **No recursive read.** One file per call. If the model wants many
  files, it can fire a parallel-safe batch.
