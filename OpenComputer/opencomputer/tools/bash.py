"""Bash tool — run a shell command with a timeout."""

from __future__ import annotations

import asyncio
import os

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

#: Hermes-parity infrastructure-var blocklist. These keys are stripped
#: from the BashTool subprocess env regardless of user passthrough.
#:
#: Rationale (Hermes spec, "What Each Sandbox Filters"): the agent's own
#: provider keys, gateway tokens, and OpenComputer-internal control vars
#: must not leak into shell-spawned children (npm install, git push,
#: arbitrary user scripts) — those callers should never need them, and
#: prompt-injected commands could exfiltrate them.
#:
#: Third-party tool API keys (NPM_TOKEN, AWS_ACCESS_KEY_ID, GH_TOKEN
#: when used by the user's own scripts) are NOT in this list — users
#: routinely need them in shell subprocesses.
_OC_INFRASTRUCTURE_VARS: frozenset[str] = frozenset({
    # OC + Hermes control vars
    # (matches every var with these prefixes too — see _strip_infra_env_vars).
    # Channel platform tokens — the gateway needs these, but user shells don't.
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "MATTERMOST_BOT_TOKEN",
    "MATRIX_ACCESS_TOKEN",
    "WHATSAPP_API_TOKEN",
    "SIGNAL_BOT_TOKEN",
    "SMS_BOT_TOKEN",
    "EMAIL_BOT_TOKEN",
    "DINGTALK_BOT_TOKEN",
    "FEISHU_BOT_TOKEN",
    "WECOM_BOT_TOKEN",
    "TEAMS_BOT_TOKEN",
    # Gateway control
    "GATEWAY_ALLOW_ALL_USERS",
    "GATEWAY_ALLOWED_USERS",
    "OPENCOMPUTER_ALLOW_ROOT_GATEWAY",
})

#: Variable-name prefixes that imply OC / Hermes infrastructure ownership.
#: Any env var starting with one of these is stripped.
_OC_INFRASTRUCTURE_PREFIXES: tuple[str, ...] = (
    "OPENCOMPUTER_",
    "HERMES_",
    "OC_",  # OpenComputer-prefixed knobs
)


def _strip_infra_env_vars(env: dict[str, str] | None) -> dict[str, str] | None:
    """Strip OC infrastructure vars from a copy of ``env``.

    Returns ``None`` unchanged so the caller's "use parent env" fallback
    still works. Only OC's own vars are removed — third-party tool keys
    (NPM_TOKEN, AWS_*, etc.) pass through untouched because user scripts
    routinely need them.
    """
    if env is None:
        return None
    out = dict(env)
    for k in list(out.keys()):
        if k in _OC_INFRASTRUCTURE_VARS:
            del out[k]
            continue
        if any(k.startswith(p) for p in _OC_INFRASTRUCTURE_PREFIXES):
            del out[k]
    return out


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

            scoped = scope_subprocess_env(
                os.environ.copy(), profile=read_active_profile()
            )
            env = _strip_infra_env_vars(scoped)
        except Exception:
            # If profile scoping fails for any reason, fall back to a
            # bare strip of the parent env so a tool-internal token can
            # never leak into the subprocess.
            env = _strip_infra_env_vars(os.environ.copy())

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
