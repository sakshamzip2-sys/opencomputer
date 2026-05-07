"""Bash tool — run a shell command with a timeout."""

from __future__ import annotations

import asyncio
import os

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class BashTool(BaseTool):
    parallel_safe = False  # side effects
    # Item 3 (2026-05-02): Bash schema enumerates command/timeout_s; closed.
    strict_mode = True

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
                "additionalProperties": False,
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
        # Scope HOME / XDG_* to the active profile's home/ subdir so
        # spawned subprocesses (git, ssh, npm, etc.) get per-profile
        # tool-config isolation for credentials and caches. The parent
        # process keeps its real HOME — see _apply_profile_override
        # in cli.py for the architectural rationale.
        try:
            from opencomputer.profiles import (
                read_active_profile,
                scope_subprocess_env,
            )

            env = scope_subprocess_env(
                os.environ.copy(), profile=read_active_profile()
            )
        except Exception:
            # If profile scoping fails for any reason, fall back to the
            # parent's env so the command still runs. BashTool MUST NOT
            # be brittle to profile lookup edge cases.
            env = None

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            exit_code = proc.returncode or 0
        except TimeoutError:
            # PR-A Feature 1: terminate the proc so partial output can
            # be captured; the call site treats timeout same as cancel.
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (TimeoutError, ProcessLookupError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: command timed out after {timeout}s",
                is_error=True,
            )
        except asyncio.CancelledError:
            # PR-A Feature 1 — Steer Replan-with-Context. The agent
            # loop's cancel-aware dispatch fires CancelledError on the
            # _run_one task; we terminate the subprocess and pre-build
            # a result with whatever stdout was captured before re-
            # raising so the loop's _make_cancelled_result helper can
            # surface partial output to the model on replan.
            partial_stdout = ""
            if proc is not None:
                try:
                    if proc.returncode is None:
                        proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=1.0)
                        except (TimeoutError, ProcessLookupError):
                            try:
                                proc.kill()
                            except ProcessLookupError:
                                pass
                    # Drain whatever already buffered. communicate() may
                    # have queued data on the pipe even though the await
                    # was cancelled.
                    try:
                        if proc.stdout is not None:
                            buf = await asyncio.wait_for(
                                proc.stdout.read(), timeout=0.5,
                            )
                            partial_stdout = buf.decode("utf-8", errors="replace")
                    except (TimeoutError, Exception):  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass
            # Stash the partial output on the cancelled task so the
            # dispatcher's _make_cancelled_result can pick it up.
            current_task = asyncio.current_task()
            if current_task is not None:
                # Attach as an attribute on the task object — read by
                # _make_cancelled_result via getattr fallback.
                try:
                    object.__setattr__(
                        current_task, "_pr_a_partial_stdout", partial_stdout,
                    )
                except (AttributeError, TypeError):
                    pass
            raise
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
