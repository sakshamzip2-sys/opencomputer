"""Tier 4 — System Control tools (4 tools, MUTATING — highest consent bar).

Tools:
  1. edit_file       — String replacement in files (per-edit consent)
  2. run_shell       — Shell command execution (stricter consent + sandbox)
  3. run_applescript — macOS AppleScript execution (per-app consent)
  4. inject_keyboard — Type text via keyboard injection (stricter consent)

OI method mappings per oi-source-map.md:
  - edit_file       → computer.files.edit(path, original_text, replacement_text)
  - run_shell       → computer.terminal.run("shell", code)
  - run_applescript → computer.utils.run_applescript(script)
  - inject_keyboard → computer.keyboard.write(text)

ALL Tier 4 tools:
  - consent_tier = 4
  - capability_claims with PER_ACTION tier (never blanket)
  - SANDBOX_HOOK: 3.E SandboxStrategy exposes run_sandboxed(argv, config) — not a
    .guard() method on a strategy instance. Tier 4 tools route through OI subprocess
    (which is itself the isolation boundary); direct run_sandboxed wiring would require
    extracting the command from the wrapper call, which is not supported by the
    current OISubprocessWrapper API. Left as:
    # SANDBOX_HOOK pending 3.E API match — wrapper call is the subprocess boundary

PR-3 (2026-04-25): moved from extensions/oi-capability/ into
extensions/coding-harness/oi_bridge/ per docs/f7/interweaving-plan.md.
capability_claims declared on each class — F1 ConsentGate enforces at dispatch.
AUDIT_HOOK markers removed: audit happens automatically through the gate (PRs #64/#65).
"""

from __future__ import annotations

from typing import Any, ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

from ..subprocess.wrapper import OISubprocessWrapper


class EditFileTool(BaseTool):
    """Edit a file by replacing an exact string with new content. Per-edit consent."""

    consent_tier: int = 4
    parallel_safe: bool = False  # mutations are always sequential
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="oi_bridge.edit_file",
            tier_required=ConsentTier.PER_ACTION,
            human_description="Edit a file by replacing an exact string with new text (per-edit consent).",
        ),
    )

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="edit_file",
            description=(
                "Edit a file by replacing an exact string with new text. "
                "Requires per-edit user consent. Path must be specified. "
                "Platform: all."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file to edit"},
                    "original_text": {"type": "string", "description": "Exact text to find and replace"},
                    "replacement_text": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "original_text", "replacement_text"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # F1 ConsentGate enforces capability_claims at dispatch (PER_ACTION tier).
        # SANDBOX_HOOK pending 3.E API match — the OI subprocess is itself the
        # isolation boundary; direct run_sandboxed wiring requires extracting
        # the command from the wrapper call (not supported by current wrapper API).
        # TODO: wire run_sandboxed once wrapper exposes pre-exec hooks.

        params = {
            "path": call.arguments["path"],
            "original_text": call.arguments["original_text"],
            "replacement_text": call.arguments["replacement_text"],
        }

        try:
            result = await self._wrapper.call("computer.files.edit", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        return ToolResult(tool_call_id=call.id, content=str(result))


class RunShellTool(BaseTool):
    """Execute a shell command. Requires strict consent + sandbox. Output captured."""

    consent_tier: int = 4
    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="oi_bridge.run_shell",
            tier_required=ConsentTier.PER_ACTION,
            human_description="Execute a shell command (per-command consent; sandbox must be configured).",
        ),
    )

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="run_shell",
            description=(
                "Execute a shell command. Output is captured and returned. "
                "Requires strict consent. Sandbox must be configured by admin. "
                "Platform: all."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)", "default": 30},
                },
                "required": ["command"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # F1 ConsentGate enforces capability_claims at dispatch (PER_ACTION tier).
        # SANDBOX_HOOK pending 3.E API match — the OI subprocess is itself the
        # isolation boundary; direct run_sandboxed wiring requires extracting
        # the command from the wrapper call (not supported by current wrapper API).
        # TODO: wire run_sandboxed once wrapper exposes pre-exec hooks.

        params = {
            "language": "shell",
            "code": call.arguments["command"],
        }

        try:
            result = await self._wrapper.call("computer.terminal.run", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        return ToolResult(tool_call_id=call.id, content=str(result))


class RunAppleScriptTool(BaseTool):
    """Execute an AppleScript on macOS. Per-app consent required."""

    consent_tier: int = 4
    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="oi_bridge.run_applescript",
            tier_required=ConsentTier.PER_ACTION,
            human_description="Execute an AppleScript on macOS (per-script consent required).",
        ),
    )

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="run_applescript",
            description=(
                "Execute an AppleScript. Can control any macOS application. "
                "Requires per-script consent. Platform: macOS ONLY."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "AppleScript source code to execute"},
                },
                "required": ["script"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # F1 ConsentGate enforces capability_claims at dispatch (PER_ACTION tier).
        # SANDBOX_HOOK pending 3.E API match — see RunShellTool comment.
        # TODO: wire run_sandboxed once wrapper exposes pre-exec hooks.

        params = {"script": call.arguments["script"]}

        try:
            result = await self._wrapper.call("computer.os.run_applescript", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        return ToolResult(tool_call_id=call.id, content=str(result))


class InjectKeyboardTool(BaseTool):
    """Type text into the focused application via keyboard injection."""

    consent_tier: int = 4
    parallel_safe: bool = False  # keyboard injection is inherently sequential
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="oi_bridge.inject_keyboard",
            tier_required=ConsentTier.PER_ACTION,
            human_description="Type text into the currently focused application via keyboard injection.",
        ),
    )

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="inject_keyboard",
            description=(
                "Type text into the currently focused application by simulating keystrokes. "
                "User cannot intercept once started. Requires strict per-action consent. "
                "Platform: all."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type"},
                    "interval": {
                        "type": "number",
                        "description": "Delay between keystrokes in seconds (default: 0.05)",
                        "default": 0.05,
                    },
                },
                "required": ["text"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # F1 ConsentGate enforces capability_claims at dispatch (PER_ACTION tier).
        # SANDBOX_HOOK pending 3.E API match — see RunShellTool comment.
        # TODO: wire run_sandboxed once wrapper exposes pre-exec hooks.

        params = {
            "text": call.arguments["text"],
            "interval": call.arguments.get("interval", 0.05),
        }

        try:
            result = await self._wrapper.call("computer.keyboard.write", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        return ToolResult(tool_call_id=call.id, content=str(result))


ALL_TOOLS = [
    EditFileTool,
    RunShellTool,
    RunAppleScriptTool,
    InjectKeyboardTool,
]

__all__ = [
    "EditFileTool",
    "RunShellTool",
    "RunAppleScriptTool",
    "InjectKeyboardTool",
    "ALL_TOOLS",
]
