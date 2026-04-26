"""PythonExec — sandboxed Python script execution.

Runs the script in a subprocess so a SystemExit / sys.exit / runaway
allocation in the script doesn't kill the agent. Output (stdout +
stderr) is captured and returned. The denylist (python_safety) blocks
obvious abuse patterns before subprocess spawn.
"""
from __future__ import annotations

import asyncio
import logging
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
    """Run a Python script in a subprocess; capture stdout + stderr."""

    consent_tier: int = 2
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="python_exec.run",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Execute a Python script in a subprocess.",
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="PythonExec",
            description=(
                "Run a Python script in a subprocess and return stdout + stderr. "
                "Use this for ad-hoc data analysis (pandas, sklearn, json transforms) "
                "where Bash + python3 -c would be clunky. Multi-line scripts welcome. "
                "Denylisted patterns (os.system, subprocess, eval, .ssh access) are "
                "rejected pre-spawn."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python source to execute. Must not contain denylisted patterns.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "default": 30.0,
                        "description": "Wall-clock timeout. Default 30s.",
                    },
                },
                "required": ["code"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        code = str(call.arguments.get("code", ""))
        timeout = float(call.arguments.get("timeout_seconds", 30.0))

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
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
