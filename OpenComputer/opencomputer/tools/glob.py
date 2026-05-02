"""Glob tool — find files by pattern, sorted by mtime."""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class GlobTool(BaseTool):
    parallel_safe = True
    # Item 3 (2026-05-02): Glob accepts only pattern/path/max_results; closed.
    strict_mode = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Glob",
            description=(
                "Find files matching a glob pattern, sorted by mtime (newest first). "
                "Supports recursive globs like '**/*.py' or 'src/**/*.ts'. Use this when "
                "you need to enumerate files by name or extension — what's in a "
                "directory, every test file, recently-touched configs. Prefer Glob over "
                "Bash `find` / `ls` / `git ls-files` since the result ordering is "
                "predictable and the harness can route paths cleanly. For content "
                "search (regex inside files), use Grep instead. Pattern is required; "
                "results are capped at `max_results` (default 500) to avoid context blow-up."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Root to search from. Defaults to cwd.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Cap the result count. Default 500.",
                    },
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        max_results = int(args.get("max_results", 500))

        if not pattern:
            return ToolResult(
                tool_call_id=call.id, content="Error: pattern required", is_error=True
            )
        root = Path(path)
        if not root.exists():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: path does not exist: {root}",
                is_error=True,
            )

        matches = list(root.glob(pattern))
        matches = [p for p in matches if p.is_file()]
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        matches = matches[:max_results]

        if not matches:
            return ToolResult(tool_call_id=call.id, content="(no matches)")
        return ToolResult(
            tool_call_id=call.id,
            content="\n".join(str(p) for p in matches),
        )
