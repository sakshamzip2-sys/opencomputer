"""Background process tools: StartProcess, CheckOutput, KillProcess.

Shared in-memory registry keyed by pid. Output is streamed into a bounded
buffer so CheckOutput can read pending lines without blocking. Processes
are cleaned up on SessionEnd to prevent zombies.

Round 2B P-8 — auto-notification on exit. Each :class:`StartProcessTool`
spawn registers a watcher coroutine that awaits ``proc.wait()`` then
fires a :class:`~plugin_sdk.hooks.HookEvent.NOTIFICATION` carrying a
:class:`~opencomputer.agent.bg_notify.BgProcessExit` payload. The default
subscriber stashes a system message in
:mod:`opencomputer.agent.bg_notify`; the agent loop drains that store
between turns so the model sees the completion in its next-turn context.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
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
    # P-8 — completion bookkeeping. ``tool_call_id`` and ``session_id``
    # are stamped at start so the watcher can route the eventual exit
    # notification back to the originating session even if the agent has
    # since moved on. ``started_at`` is ``time.monotonic()`` so the
    # duration calc is immune to NTP slews.
    tool_call_id: str = ""
    session_id: str = ""
    started_at: float = 0.0
    notify_task: asyncio.Task | None = None


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


async def _watch_and_notify(entry: _BgEntry) -> None:
    """P-8 — await proc exit, then fire the Notification hook.

    Defensive: every step is wrapped because this runs as a fire-and-
    forget task under the agent's event loop. A raise here would surface
    as a "Task exception was never retrieved" warning AND silently drop
    the agent's auto-notification, so we'd rather log + move on than
    propagate.

    The watcher waits for the read-drain tasks BEFORE constructing the
    payload so the stdout / stderr buffers reflect everything the
    process actually wrote — readline returns EOF only after the pipe
    closes, which is after the process exits. Without this wait the
    tail strings would race and could miss the last few hundred bytes.
    """
    try:
        await entry.proc.wait()
    except Exception:  # noqa: BLE001
        logger.warning("bg watcher: proc.wait raised for pid=%d", entry.pid, exc_info=True)
        return

    # Drain any remaining buffered output. ``_drain`` returns when the
    # stream EOFs — both should EOF shortly after the process exits.
    for t in (entry.read_task_out, entry.read_task_err):
        if t is None:
            continue
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except (TimeoutError, Exception):  # noqa: BLE001
            # Don't let a stuck reader block the notification.
            t.cancel()

    duration = max(0.0, time.monotonic() - entry.started_at)
    exit_code = entry.proc.returncode if entry.proc.returncode is not None else -1

    # Build payload + fire hook. Imports are local because background.py
    # is loaded by the plugin loader's synthetic-module path; importing
    # opencomputer.* at module top would require sys.path setup the loader
    # already provides at register time, but local imports keep test
    # isolation cleaner (each test's _load_module call is self-contained).
    try:
        from opencomputer.agent.bg_notify import (
            BgProcessExit,
            make_hook_context,
            tail_chars,
        )
        from opencomputer.hooks.engine import engine as _hook_engine

        payload = BgProcessExit(
            session_id=entry.session_id,
            tool_call_id=entry.tool_call_id,
            exit_code=exit_code,
            tail_stdout=tail_chars(list(entry.stdout_lines), 200),
            tail_stderr=tail_chars(list(entry.stderr_lines), 200),
            duration_seconds=duration,
        )
        ctx = make_hook_context(payload)
        # ``fire_and_forget`` schedules every Notification subscriber.
        # The bg_notify default subscriber stashes the system message;
        # any user-side Notification subscribers (Telegram mirroring,
        # audit logging) see the same context and can opt-in / out via
        # the BG_PROCESS_EXIT_MARKER name on ctx.message.
        _hook_engine.fire_and_forget(ctx)
    except Exception:  # noqa: BLE001
        logger.warning(
            "bg watcher: notification fire failed for pid=%d", entry.pid, exc_info=True
        )


async def _cleanup_all() -> None:
    """Called on SessionEnd to kill any lingering processes.

    P-8 — also cancels each entry's notify watcher. A SessionEnd-driven
    cleanup means the agent is going away; firing a Notification into a
    dead session would be wasted work (the drain side is keyed on
    session id, so the system message would never be consumed). Cancel
    explicitly so the watcher returns cleanly without producing a stale
    payload.
    """
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
            if entry.notify_task is not None and not entry.notify_task.done():
                entry.notify_task.cancel()
            _PROCESSES.pop(entry.pid, None)


def count_running_processes() -> int:
    """Return how many bg processes are currently tracked.

    Public read-only API for the ``/stop`` slash command. Hermes-parity
    Tier A (2026-04-30).
    """
    return sum(
        1 for entry in _PROCESSES.values()
        if entry.proc.returncode is None
    )


async def stop_all_processes() -> int:
    """Kill every tracked bg process. Returns count of processes killed.

    Public API exposed for the ``/stop`` slash command. Hermes-parity
    Tier A (2026-04-30). Mirrors ``_cleanup_all`` semantics but returns
    the count so the slash handler can report it to the user.
    """
    before = count_running_processes()
    await _cleanup_all()
    return before


class StartProcessTool(BaseTool):
    parallel_safe = False

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="StartProcess",
            description=(
                "Start a long-running process in the background; return its pid. Use "
                "for dev servers, test watchers, log tailers, anything that doesn't "
                "exit on its own. The pid lets you later call CheckOutput (read "
                "stdout/stderr) and KillProcess (terminate). Prefer StartProcess over "
                "Bash for any command that won't terminate quickly — Bash blocks the "
                "agent loop until the timeout, while StartProcess returns immediately. "
                "CAUTION: spawned processes inherit the agent's environment; output is "
                "buffered to a 5000-line ring per stream so very chatty processes lose "
                "old lines. The watcher fires a Notification hook on exit so the agent "
                "sees completion in its next turn even without polling."
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
            from opencomputer.profiles import read_active_profile, scope_subprocess_env

            env = scope_subprocess_env(
                os.environ.copy(), profile=read_active_profile()
            )
        except Exception:  # noqa: BLE001 — fail-soft on profile lookup
            env = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error starting process: {type(e).__name__}: {e}",
                is_error=True,
            )
        # P-8 — capture context the watcher needs at start time. We grab
        # the active session id BEFORE spawning the watcher so a session
        # that ends mid-run still routes the eventual exit notification
        # to the original session (the agent loop's drain is keyed on id,
        # not "currently active session"; a dead session sees no harm).
        try:
            from opencomputer.agent.bg_notify import current_session_id

            session_id = current_session_id()
        except Exception:  # noqa: BLE001 — defensive
            session_id = ""
        entry = _BgEntry(
            pid=proc.pid,
            proc=proc,
            command=cmd,
            tool_call_id=call.id,
            session_id=session_id,
            started_at=time.monotonic(),
        )
        entry.read_task_out = asyncio.create_task(_drain(proc.stdout, entry.stdout_lines))
        entry.read_task_err = asyncio.create_task(_drain(proc.stderr, entry.stderr_lines))
        # P-8 — auto-notify watcher. Fire-and-forget; the task is held on
        # the entry so KillProcess + cleanup can cancel it explicitly to
        # avoid a "Task was destroyed but pending" warning at shutdown.
        entry.notify_task = asyncio.create_task(_watch_and_notify(entry))
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
            name="CheckOutput",
            description=(
                "Read pending stdout/stderr lines from a background process by pid. "
                "Use after StartProcess to see what the process has written so far — "
                "build output, server logs, test progress. By default `drain=true` "
                "consumes the buffer (clears read lines); pass `drain=false` to peek "
                "without consuming. Output is split into stdout/stderr sections with a "
                "status header (`running` or `exited(code=N)`) so you can tell whether "
                "to keep watching or move on. Read-only and parallel-safe. If the pid "
                "isn't tracked the call errors — likely the process was already killed "
                "or the agent restarted."
            ),
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
            name="KillProcess",
            description=(
                "Terminate a background process by pid (started via StartProcess). "
                "Sends SIGTERM first, escalates to SIGKILL after a 3-second grace "
                "period if the process doesn't exit. Use this when a dev server or "
                "watcher is no longer needed, or when a runaway process is consuming "
                "resources. CAUTION: this is a hard stop — any in-flight work the "
                "process was doing is lost. Prefer letting short-lived commands "
                "complete on their own; KillProcess is for processes that won't exit "
                "otherwise. If the pid isn't tracked the call errors (already killed, "
                "or session restart cleaned it up)."
            ),
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
    "count_running_processes",
    "stop_all_processes",
]
