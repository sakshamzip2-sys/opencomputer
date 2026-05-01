"""PythonExec — sandboxed Python script execution + PTC mode.

Two modes:

- **Default ``mode="plain"``**: runs the script in a subprocess so a
  SystemExit / runaway allocation in the script doesn't kill the
  agent. Output (stdout + stderr) is captured and returned. The
  denylist (python_safety) blocks obvious abuse patterns pre-spawn.

- **``mode="ptc"``** (Tier-A item 8): Programmatic Tool Calling. The
  subprocess gets a small RPC harness prepended to its code so it
  can call OC's registered tools (Read, WebFetch, Grep, Glob by
  default — caller can extend via ``tools=[...]``). Each tool call
  is a synchronous round-trip to the parent over a per-invocation
  Unix domain socket. Only the script's stdout returns to the LLM —
  intermediate tool results never enter the conversation context.

  Use case: "summarize and combine these 5 articles" collapses from
  10 round-trips (5 fetches + interpretation + summary) to ONE
  inference turn. The script does the orchestration; the LLM sees
  the final answer.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

from opencomputer.security.python_safety import is_safe_script
from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_log = logging.getLogger("opencomputer.tools.python_exec")


class PythonExec(BaseTool):
    """Run a Python script in a subprocess; capture stdout + stderr.

    With ``mode="ptc"`` the subprocess can call OC's registered tools
    via UDS-RPC — see module docstring for the orchestration value.
    """

    consent_tier: int = 2
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="python_exec.run",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Execute a Python script in a subprocess.",
        ),
        CapabilityClaim(
            capability_id="python_exec.ptc_mode",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Programmatic Tool Calling — script can call OC tools via "
                "RPC. Higher trust tier than plain execution."
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="PythonExec",
            description=(
                "Run a multi-line Python script in a subprocess; returns stdout+stderr "
                "and exit code. Use for ad-hoc data analysis (pandas, sklearn, numpy), "
                "JSON transforms, regex experiments, throwaway computations — anything "
                "where `Bash python3 -c '...'` quoting would be painful. Prefer this over "
                "Bash for non-trivial Python: multi-line scripts execute cleanly and the "
                "denylist (os.system, subprocess, eval, exec, .ssh access) is reviewed "
                "pre-spawn so obvious foot-guns are caught early.\n\n"
                "PTC MODE (mode='ptc'): your script gets predefined functions for "
                "OC's tools — Read(file_path), WebFetch(url), Grep(pattern, path), "
                "Glob(pattern). Each call returns the tool's text output as a string; "
                "errors raise RuntimeError. Use this when you need to orchestrate "
                "MULTIPLE tool calls that condition on each other in ONE inference "
                "turn — e.g. 'fetch these 5 URLs in parallel and combine'. Only the "
                "script's final stdout is returned to you; intermediate tool outputs "
                "never enter context. Default tools list is read-only (Read, WebFetch, "
                "Grep, Glob); pass `tools=['Read','Bash']` to expand. CAUTION: each "
                "PTC invocation requires per-action consent. CAPS: 50 RPC calls per "
                "script, 50 KB stdout, 300s wallclock."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python source to execute. Must not contain denylisted patterns.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["plain", "ptc"],
                        "default": "plain",
                        "description": (
                            "'plain' = subprocess, no tool RPC; 'ptc' = "
                            "subprocess with OC tool stubs predefined."
                        ),
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "PTC mode only — explicit tool allowlist. Default "
                            "(if omitted): Read, WebFetch, Grep, Glob."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "default": 30.0,
                        "description": "Wall-clock timeout. Default 30s; PTC mode allows up to 300s.",
                    },
                },
                "required": ["code"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        code = str(call.arguments.get("code", ""))
        mode = str(call.arguments.get("mode", "plain")).lower()
        timeout = float(call.arguments.get("timeout_seconds", 30.0))

        if mode == "ptc":
            return await self._execute_ptc(call, code, timeout)
        return await self._execute_plain(call, code, timeout)

    async def _execute_plain(
        self, call: ToolCall, code: str, timeout: float,
    ) -> ToolResult:
        if not is_safe_script(code):
            return ToolResult(
                tool_call_id=call.id,
                content="Script rejected by denylist (unsafe pattern detected). Avoid os.system, subprocess, eval, exec, /.ssh/, etc.",
                is_error=True,
            )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8",
        ) as f:
            f.write(code)
            script_path = Path(f.name)

        try:
            from opencomputer.profiles import read_active_profile, scope_subprocess_env

            env = scope_subprocess_env(
                os.environ.copy(), profile=read_active_profile()
            )
        except Exception:  # noqa: BLE001 — fail-soft on profile lookup
            env = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Timed out after {timeout}s",
                    is_error=True,
                )

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            combined = (stdout + "\n" + stderr).strip()

            if proc.returncode != 0:
                return ToolResult(
                    tool_call_id=call.id,
                    content=combined or f"Exit code {proc.returncode}",
                    is_error=True,
                )

            return ToolResult(
                tool_call_id=call.id, content=combined or "(no output)",
            )
        finally:
            try:
                script_path.unlink()
            except OSError:
                pass

    async def _execute_ptc(
        self, call: ToolCall, code: str, timeout: float,
    ) -> ToolResult:
        """PTC mode — subprocess can call registered tools via UDS-RPC.

        We DO NOT run the python_safety denylist here: the user's
        script is *expected* to call our tools via the RPC stubs. The
        safety surface is instead:

        - allowlisted tool set (default read-only)
        - per-action consent gate (``python_exec.ptc_mode``)
        - 50 KB stdout, 50 RPC calls, 300s wallclock

        Imports lazily so the module load cost (asyncio UDS server,
        struct, etc.) is paid only when PTC is actually used.
        """
        from opencomputer.tools.ptc import DEFAULT_ALLOWED_TOOLS, run_ptc
        from opencomputer.tools.registry import registry

        tools_arg = call.arguments.get("tools")
        allowed: tuple[str, ...]
        # Honour an explicit empty list (caller wants NO tool stubs)
        # vs absent / non-list (use defaults).
        if isinstance(tools_arg, list):
            allowed = tuple(str(t) for t in tools_arg)
        else:
            allowed = DEFAULT_ALLOWED_TOOLS

        # PTC mode wallclock cap is higher than plain (300s default vs 30s).
        if timeout < 60:
            timeout = 60.0
        timeout = min(timeout, 300.0)

        result = await run_ptc(
            code,
            registry=registry,
            allowed_tools=allowed,
            timeout_s=timeout,
        )

        if result.timed_out:
            return ToolResult(
                tool_call_id=call.id,
                content=f"PTC script timed out after {timeout}s",
                is_error=True,
            )
        if result.exit_code != 0:
            combined = (result.stdout + "\n" + result.stderr).strip()
            return ToolResult(
                tool_call_id=call.id,
                content=combined or f"PTC script exited {result.exit_code}",
                is_error=True,
            )

        out = result.stdout.strip()
        meta = (
            f"\n\n[ptc: {result.rpc_call_count} tool call(s), "
            f"{result.duration_seconds:.1f}s, "
            f"{'truncated' if result.truncated else 'full'}]"
        )
        return ToolResult(
            tool_call_id=call.id,
            content=(out or "(no output)") + meta,
        )
