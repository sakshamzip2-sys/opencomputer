"""Grep tool — search file contents with ripgrep if available, else Python fallback."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class GrepTool(BaseTool):
    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Grep",
            description=(
                "Search for a regex pattern in files. Uses ripgrep if available, "
                "falls back to pure Python. Returns matching lines with file:line prefix."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in. Defaults to cwd.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Optional glob filter (e.g. '*.py').",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "Case-insensitive match (-i). Default false.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Cap the number of matches. Default 200.",
                    },
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        glob = args.get("glob", "")
        case_i = bool(args.get("case_insensitive", False))
        max_results = int(args.get("max_results", 200))

        if not pattern:
            return ToolResult(tool_call_id=call.id, content="Error: pattern required", is_error=True)

        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "--no-heading", "--line-number", "--color=never"]
            if case_i:
                cmd.append("-i")
            if glob:
                cmd += ["--glob", glob]
            cmd += ["--max-count", str(max_results)]
            cmd += [pattern, path]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            txt = (out or b"").decode("utf-8", errors="replace")
            return ToolResult(
                tool_call_id=call.id,
                content=txt or "(no matches)",
            )

        # pure-python fallback
        try:
            regex = re.compile(pattern, re.IGNORECASE if case_i else 0)
        except re.error as e:
            return ToolResult(
                tool_call_id=call.id, content=f"Error: invalid regex: {e}", is_error=True
            )

        target = Path(path)
        files: list[Path]
        if target.is_file():
            files = [target]
        else:
            files = list(target.rglob(glob or "*"))
            files = [f for f in files if f.is_file()]

        hits: list[str] = []
        for f in files:
            if len(hits) >= max_results:
                break
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{f}:{i}:{line}")
                        if len(hits) >= max_results:
                            break
            except Exception:
                continue

        return ToolResult(
            tool_call_id=call.id,
            content="\n".join(hits) or "(no matches)",
        )
