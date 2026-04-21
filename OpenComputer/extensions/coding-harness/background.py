"""Background process tools: start_process, check_output, kill_process.

Shared in-memory registry keyed by pid. Output is streamed into a bounded
buffer so check_output can read pending lines without blocking. Processes
are cleaned up on SessionEnd to prevent zombies.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections import deque
from dataclasses import dataclass, field

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

logger = logging.getLogger("opencomputer.ext.coding_harness.background")


@dataclass(slots=True)
class _BgEntry:
    pid: int
    proc: asyncio.subprocess.Process
    stdout_lines: deque[str] = field(default_factory=lambda: deque(maxlen=5000))
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=5000))
    read_task_out: asyncio.Task | None = None
    read_task_err: asyncio.Task | None = None
    command: str = ""


#: Process table — module-level singleton, shared across all tool instances.
_PROCESSES: dict[int, _BgEntry] = {}


async def _drain(stream: asyncio.StreamReader | None, buf: deque[str]) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        buf.append(line.decode("utf-8", errors="replace").rstrip("\n"))


async def _cleanup_all() -> None:
    """Called on SessionEnd to kill any lingering processes."""
    for entry in list(_PROCESSES.values()):
        try:
            if entry.proc.returncode is None:
                entry.proc.terminate()
                try:
                    await asyncio.wait_for(entry.proc.wait(), timeout=3.0)
                except TimeoutError:
                    entry.proc.kill()
        except ProcessLookupError:
            pass
        finally:
            _PROCESSES.pop(entry.pid, None)


class StartProcessTool(BaseTool):
    parallel_safe = False

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="start_process",
            description=(
                "Start a long-running process in the background and return its pid. "
                "Use for dev servers, test watchers, any command you'll want to check "
                "on later. The pid lets you call check_output and kill_process."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to launch (e.g. 'npm run dev').",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (default: current).",
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        cmd = call.arguments.get("command", "").strip()
        cwd = call.arguments.get("cwd") or None
        if not cmd:
            return ToolResult(
                tool_call_id=call.id, content="Error: empty command", is_error=True
            )
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error starting process: {type(e).__name__}: {e}",
                is_error=True,
            )
        entry = _BgEntry(pid=proc.pid, proc=proc, command=cmd)
        entry.read_task_out = asyncio.create_task(_drain(proc.stdout, entry.stdout_lines))
        entry.read_task_err = asyncio.create_task(_drain(proc.stderr, entry.stderr_lines))
        _PROCESSES[proc.pid] = entry
        return ToolResult(
            tool_call_id=call.id,
            content=f"Started process (pid={proc.pid}): {cmd}",
        )


class CheckOutputTool(BaseTool):
    parallel_safe = True  # readonly

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="check_output",
            description="Read pending stdout/stderr lines from a background process by pid.",
            parameters={
                "type": "object",
                "properties": {
                    "pid": {"type": "integer"},
                    "drain": {
                        "type": "boolean",
                        "description": "If true, consume the buffer (default). If false, peek.",
                    },
                },
                "required": ["pid"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        pid = int(call.arguments.get("pid", 0))
        drain = bool(call.arguments.get("drain", True))
        entry = _PROCESSES.get(pid)
        if entry is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: no background process with pid={pid}",
                is_error=True,
            )

        if drain:
            out_lines = [entry.stdout_lines.popleft() for _ in range(len(entry.stdout_lines))]
            err_lines = [entry.stderr_lines.popleft() for _ in range(len(entry.stderr_lines))]
        else:
            out_lines = list(entry.stdout_lines)
            err_lines = list(entry.stderr_lines)

        status = (
            f"exited(code={entry.proc.returncode})"
            if entry.proc.returncode is not None
            else "running"
        )
        parts = [f"[{status}] pid={pid} cmd={entry.command}"]
        if out_lines:
            parts.append("--- stdout ---")
            parts.extend(out_lines)
        if err_lines:
            parts.append("--- stderr ---")
            parts.extend(err_lines)
        if not out_lines and not err_lines:
            parts.append("(no new output)")
        return ToolResult(tool_call_id=call.id, content="\n".join(parts))


class KillProcessTool(BaseTool):
    parallel_safe = False

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="kill_process",
            description="Terminate a background process by pid.",
            parameters={
                "type": "object",
                "properties": {"pid": {"type": "integer"}},
                "required": ["pid"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        pid = int(call.arguments.get("pid", 0))
        entry = _PROCESSES.pop(pid, None)
        if entry is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: no background process with pid={pid}",
                is_error=True,
            )
        try:
            if entry.proc.returncode is None:
                entry.proc.terminate()
                try:
                    await asyncio.wait_for(entry.proc.wait(), timeout=3.0)
                except TimeoutError:
                    entry.proc.kill()
        except ProcessLookupError:
            pass
        return ToolResult(
            tool_call_id=call.id, content=f"Killed pid={pid}"
        )


__all__ = [
    "StartProcessTool",
    "CheckOutputTool",
    "KillProcessTool",
    "_PROCESSES",
    "_cleanup_all",
]
