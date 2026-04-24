# Example — a destructive tool that must NOT be parallel-safe

A tool that modifies files (or anything else with ordering constraints)
MUST leave `parallel_safe = False`. Two concurrent edits to the same
file race; two concurrent shell commands can deadlock each other. The
loop's three-layer gate catches most mistakes, but the responsibility
starts with the tool author.

```python
"""RenamePath — atomically rename a file."""

from __future__ import annotations

import os
from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class RenamePathTool(BaseTool):
    #: Destructive — two RenamePath calls in the same batch could race
    #: on the destination path and one will silently lose its data.
    #: Leave False. The agent loop will sequence these.
    parallel_safe = False

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="RenamePath",
            description=(
                "Rename (move) a file or directory. Atomic on POSIX. "
                "Fails if the destination already exists — this tool "
                "will not silently overwrite. Use only when you are "
                "sure the rename is correct."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "src": {
                        "type": "string",
                        "description": "Absolute path of the existing file or directory.",
                    },
                    "dst": {
                        "type": "string",
                        "description": (
                            "Absolute destination path. Must not already exist. "
                            "The parent directory must exist."
                        ),
                    },
                },
                "required": ["src", "dst"],
                "additionalProperties": False,
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        src = call.arguments.get("src")
        dst = call.arguments.get("dst")
        for key, val in (("src", src), ("dst", dst)):
            if not isinstance(val, str) or not val:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Error: {key!r} is required and must be a non-empty string.",
                    is_error=True,
                )

        src_p = Path(src)
        dst_p = Path(dst)
        if not src_p.is_absolute() or not dst_p.is_absolute():
            return ToolResult(
                tool_call_id=call.id,
                content="Error: both 'src' and 'dst' must be absolute paths.",
                is_error=True,
            )
        if not src_p.exists():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: source {src!r} does not exist.",
                is_error=True,
            )
        if dst_p.exists():
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: destination {dst!r} already exists. "
                    "This tool refuses to overwrite."
                ),
                is_error=True,
            )
        if not dst_p.parent.is_dir():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: destination parent {dst_p.parent!r} is not a directory.",
                is_error=True,
            )

        try:
            os.rename(src, dst)
        except OSError as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: rename failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        return ToolResult(
            tool_call_id=call.id,
            content=f"Renamed {src_p.name} -> {dst}",
        )


def register(api) -> None:
    api.register_tool(RenamePathTool())
```

## What "parallel-safe" does not mean

- `parallel_safe = True` does NOT mean "it's fine if this runs twice"
  — it means "it's fine if this runs **concurrently** with another
  parallel-safe tool".
- A tool that runs exactly once per call, sequentially with the rest,
  is correct with `parallel_safe = False`. That's the safe default.

## The three-layer gate in the loop

Even if a plugin author incorrectly marks a destructive tool parallel-safe,
`opencomputer/agent/loop.py::_all_parallel_safe` stacks two more layers
on top of the per-tool flag:

1. **Hardcoded never-parallel names** (`HARDCODED_NEVER_PARALLEL`):
   `Bash`, `AskUserQuestion`, `ExitPlanMode`, `TodoWrite`. These
   ALWAYS run sequentially regardless of the flag — catches author
   mistakes.
2. **Path-scoped tools** (`PATH_SCOPED`): `Edit`, `MultiEdit`, `Write`,
   `NotebookEdit`. Two calls on the SAME path run sequentially; on
   different paths they may parallelize.
3. **Bash destructive-command scan**: even if `Bash` were not
   hardcoded, any command matching `opencomputer/tools/bash_safety`
   patterns (`rm -rf /`, etc.) forces sequential.

If you add a new tool whose name belongs in the hardcoded list, the
edit goes in `opencomputer/agent/loop.py` — don't try to enforce it
inside your tool alone. But the starting discipline is: **default
parallel_safe to False, and only flip it on for pure reads.**

## Related: use a hook, not a flag, for mode gates

If your tool should be refused in plan mode, don't check `plan_mode`
inside `execute`. Write a `PreToolUse` hook with a matcher pattern
that covers the tool name and returns `HookDecision(decision="block",
reason="destructive tool refused in plan mode")`. Mode logic belongs
in hooks. See the `opencomputer-hook-authoring` skill for concrete
patterns.
