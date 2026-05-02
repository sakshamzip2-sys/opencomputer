"""PowerShellRun tool — execute a PowerShell script via pwsh/powershell.

Windows-only at the OS level. macOS/Linux installations of PowerShell
exist (cross-platform PowerShell Core / pwsh), but the agent's
PowerShell-targeting skills assume Windows semantics (Win32 cmdlets,
COM objects, etc.) so we hard-gate to Windows.

Mirrors ``AppleScriptRun`` in shape: PER_ACTION consent, captures
stdout/stderr, surfaces non-zero exit as ``is_error=True``.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from typing import ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_TIMEOUT_SECONDS = 30


class PowerShellRunTool(BaseTool):
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True
    """Run a PowerShell script via ``pwsh`` (preferred) or ``powershell.exe``."""

    # parallel_safe = False mirrors AppleScriptRun: PowerShell can mutate
    # registry / services / COM state, so two parallel calls would race.
    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="gui.powershell_run",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Execute a PowerShell script (Windows). Can read files, "
                "control apps via COM, query system info, modify the "
                "registry — same surface area as a manual PowerShell session."
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="PowerShellRun",
            description=(
                "Run a PowerShell script via pwsh (PowerShell 7+, preferred) "
                "or powershell.exe (Windows PowerShell 5.1, fallback). Windows "
                "only — returns an error on macOS/Linux. Captures stdout + "
                "stderr; non-zero exit is surfaced as is_error. PER_ACTION "
                "consent. Mirrors AppleScriptRun for Mac."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "PowerShell script body. Multi-line OK.",
                    },
                },
                "required": ["script"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        script = call.arguments.get("script", "")
        if not isinstance(script, str) or not script.strip():
            return ToolResult(
                tool_call_id=call.id,
                content="script must be a non-empty string",
                is_error=True,
            )

        if sys.platform != "win32":
            return ToolResult(
                tool_call_id=call.id,
                content="PowerShellRun requires Windows (sys.platform == 'win32')",
                is_error=True,
            )

        exe = shutil.which("pwsh") or shutil.which("powershell")
        if exe is None:
            return ToolResult(
                tool_call_id=call.id,
                content="neither pwsh nor powershell.exe found on PATH",
                is_error=True,
            )

        try:
            # scope_subprocess_env not needed: -NoProfile flag explicitly
            # bypasses the user's PowerShell profile.
            proc = await asyncio.to_thread(
                subprocess.run,
                [exe, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_call_id=call.id,
                content=f"PowerShell timed out after {_TIMEOUT_SECONDS}s",
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(
                tool_call_id=call.id,
                content=f"PowerShell launch failed: {exc}",
                is_error=True,
            )

        body = proc.stdout
        if proc.stderr:
            body += f"\n[stderr]\n{proc.stderr}"
        return ToolResult(
            tool_call_id=call.id,
            content=body or "(no output)",
            is_error=proc.returncode != 0,
        )


__all__ = ["PowerShellRunTool"]
