"""Diff tool — shell out to `git diff` for plain, ref, or staged diffs.

Why shell out instead of using GitPython / pygit2:
- Zero new deps. `git` is already on every dev's machine.
- Output format is what humans + LLMs already understand. No translation.
- Safer (no library-level access to repo internals).

Args:
    path:     File or directory to diff. Optional — defaults to repo root.
    against:  Ref to diff against ("HEAD", "main", commit hash, "" for working).
              Default is "" which shows working-tree changes.
    staged:   If true, show `git diff --cached` (index vs HEAD). Default false.
    max_lines: Cap on diff lines returned. Default 2000.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

DEFAULT_MAX_LINES = 2_000


def _truncate(text: str, max_lines: int) -> tuple[str, bool]:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    truncated = "\n".join(lines[:max_lines])
    omitted = len(lines) - max_lines
    return (
        f"{truncated}\n\n[truncated — {omitted} more diff lines omitted; "
        f"raise max_lines to see them]",
        True,
    )


class DiffTool(BaseTool):
    parallel_safe = True  # read-only

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Diff",
            description=(
                "Show git diff for the current repository. Three modes:\n"
                "- Working diff (default): unstaged changes vs HEAD\n"
                "- Staged diff (`staged=true`): index vs HEAD\n"
                "- Ref diff (`against=<ref>`): working tree vs <ref>\n"
                "Use this BEFORE Edit / MultiEdit / Bash to understand the "
                "current state of the repo. Read-only — never mutates anything."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "File or directory inside the repo. "
                            "Defaults to repo root (all changes)."
                        ),
                    },
                    "against": {
                        "type": "string",
                        "description": (
                            "Ref to diff against (e.g. 'HEAD', 'main', a "
                            "commit hash). Empty (default) shows working-tree "
                            "changes."
                        ),
                    },
                    "staged": {
                        "type": "boolean",
                        "description": (
                            "If true, show staged (cached) diff instead of "
                            "working diff. Mutually exclusive with 'against'."
                        ),
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": (
                            f"Cap the diff at this many lines. Default {DEFAULT_MAX_LINES}."
                        ),
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args: dict[str, Any] = call.arguments
        path = str(args.get("path", "")).strip()
        against = str(args.get("against", "")).strip()
        staged = bool(args.get("staged", False))
        max_lines = int(args.get("max_lines", DEFAULT_MAX_LINES))

        git_bin = shutil.which("git")
        if git_bin is None:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: `git` not found on PATH",
                is_error=True,
            )

        if staged and against:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: `staged` and `against` are mutually exclusive",
                is_error=True,
            )

        cmd: list[str] = [git_bin, "diff", "--no-color"]
        if staged:
            cmd.append("--cached")
        if against:
            cmd.append(against)
        if path:
            cmd.append("--")
            cmd.append(path)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_text = (stderr or b"").decode("utf-8", errors="replace").strip()
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: git diff exited {proc.returncode}: {err_text}",
                is_error=True,
            )

        diff_text = (stdout or b"").decode("utf-8", errors="replace")
        if not diff_text.strip():
            mode = "staged" if staged else (f"against {against}" if against else "working tree")
            return ToolResult(
                tool_call_id=call.id,
                content=f"(no changes — {mode})",
            )

        capped, was_truncated = _truncate(diff_text, max_lines)
        return ToolResult(tool_call_id=call.id, content=capped)


__all__ = ["DiffTool"]
