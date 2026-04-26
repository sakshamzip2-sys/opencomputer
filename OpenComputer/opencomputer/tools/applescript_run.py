"""AppleScriptRun tool — execute an AppleScript via osascript.

Phase 2.2 of the catch-up plan (real-gui-velvet-lemur). Pairs with
PointAndClickTool to give the agent practical macOS automation:
notifications, opening apps, controlling Notes/Reminders, etc.

Safety
------

- macOS only.
- PER_ACTION consent tier (F1 ConsentGate prompts every call).
- Destructive-keyword denylist (refused outright):
  ``empty trash``, ``shutdown``, ``restart``, ``delete``, ``rm -rf``,
  ``format``, ``eject``. Match is case-insensitive, word-boundary.
- ``dry_run=True`` (default False) returns the script without executing.
- Configurable timeout (1-60 seconds, default 15).
- Output captured stdout; stderr surfaced on non-zero exit.

Trust model: the user has *explicitly* enabled a PER_ACTION capability.
The denylist is defence-in-depth, not the primary gate. Truly hostile
scripts can find ways around any keyword filter — the gate is consent.
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# Word-boundary anchored, case-insensitive. Each pattern matches
# obviously-destructive keywords that an agent should never use without
# a much heavier review than the PER_ACTION prompt provides.
_DENYLIST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bempty\s+trash\b", re.IGNORECASE),
    re.compile(r"\bshutdown\b", re.IGNORECASE),
    re.compile(r"\brestart\b", re.IGNORECASE),
    re.compile(r"\brm\s+-rf?\b", re.IGNORECASE),
    re.compile(r"\bformat\b", re.IGNORECASE),
    re.compile(r"\beject\b", re.IGNORECASE),
    re.compile(r"\bdelete\b", re.IGNORECASE),
)


class AppleScriptRunTool(BaseTool):
    """Run an AppleScript snippet via ``osascript``. macOS only."""

    parallel_safe: bool = False  # GUI scripting is not parallel-safe
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="gui.applescript_run",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Execute an AppleScript via osascript. Destructive "
                "keywords are refused outright; per-action consent."
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="AppleScriptRun",
            description=(
                "Run an AppleScript on macOS via osascript. Returns stdout "
                "on success. Destructive keywords (empty trash / shutdown / "
                "restart / delete / format / eject / rm -rf) are refused. "
                "Set dry_run=true to return the script without executing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "AppleScript source.",
                        "maxLength": 8000,
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true, return the script without running it.",
                        "default": False,
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Wall-clock cap (1-60, default 15).",
                        "minimum": 1,
                        "maximum": 60,
                        "default": 15,
                    },
                },
                "required": ["script"],
            },
        )

    @staticmethod
    def _denylist_check(script: str) -> str | None:
        """Return the matched denylist pattern (for the error msg), else None."""
        for pat in _DENYLIST_PATTERNS:
            if pat.search(script):
                return pat.pattern
        return None

    async def execute(self, call: ToolCall) -> ToolResult:
        if sys.platform != "darwin":
            return ToolResult(
                tool_call_id=call.id,
                content="Error: AppleScriptRun is macOS-only.",
                is_error=True,
            )

        args = call.arguments
        script = (args.get("script") or "").strip()
        if not script:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: script is required and must be non-empty",
                is_error=True,
            )

        bad = self._denylist_check(script)
        if bad:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: refused — script contains denylisted "
                    f"keyword (pattern={bad!r}). If you need this, "
                    "use BashTool with explicit consent instead."
                ),
                is_error=True,
            )

        dry_run = bool(args.get("dry_run", False))
        if dry_run:
            return ToolResult(
                tool_call_id=call.id,
                content=f"DRY RUN — would execute:\n{script}",
            )

        try:
            timeout = int(args.get("timeout_seconds", 15))
        except (TypeError, ValueError):
            timeout = 15
        timeout = max(1, min(60, timeout))

        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: osascript timed out after {timeout}s",
                is_error=True,
            )

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: osascript exited {proc.returncode}: "
                    f"{err or '(no stderr)'}"
                ),
                is_error=True,
            )

        return ToolResult(
            tool_call_id=call.id,
            content=stdout.decode("utf-8", errors="replace").strip(),
        )
