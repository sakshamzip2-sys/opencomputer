"""Bash tool — run a shell command with a timeout."""

from __future__ import annotations

import asyncio

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class BashTool(BaseTool):
    parallel_safe = False  # side effects

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Bash",
            description=(
                "Execute a bash command in /bin/bash with a configurable timeout, "
                "returning stdout+stderr+exit code. Use for git, package managers (npm/"
                "pip/uv/cargo), build/test runners, and any shell pipeline. CAUTION: "
                "this can mutate the filesystem and run arbitrary code — review the "
                "command before invoking. Prefer Read/Edit/Write/Grep/Glob over Bash "
                "'cat'/'sed'/'echo'/'grep'/'find' since the dedicated tools give you "
                "line-numbered output and structured errors. Avoid long sleeps, "
                "interactive prompts, and `git push --force` unless explicitly asked. "
                "For long-running processes, use StartProcess instead so the call "
                "doesn't block the agent loop."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute.",
                    },
                    "timeout_s": {
                        "type": "integer",
                        "description": "Max execution time in seconds (default 60, max 600).",
                        "minimum": 1,
                        "maximum": 600,
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        cmd = args.get("command", "")
        timeout = min(int(args.get("timeout_s", 60)), 600)
        if not cmd.strip():
            return ToolResult(
                tool_call_id=call.id, content="Error: empty command", is_error=True
            )
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            exit_code = proc.returncode or 0
        except TimeoutError:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: command timed out after {timeout}s",
                is_error=True,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {type(e).__name__}: {e}",
                is_error=True,
            )

        out = stdout.decode("utf-8", errors="replace") if stdout else ""
        err = stderr.decode("utf-8", errors="replace") if stderr else ""
        combined = (
            f"$ {cmd}\n"
            f"exit={exit_code}\n"
            f"--- stdout ---\n{out}"
            + (f"\n--- stderr ---\n{err}" if err else "")
        )
        return ToolResult(
            tool_call_id=call.id, content=combined, is_error=exit_code != 0
        )
