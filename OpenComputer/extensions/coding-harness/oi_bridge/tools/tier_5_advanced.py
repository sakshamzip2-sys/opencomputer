"""Tier 5 — Advanced tools (3 tools, niche, per-action consent).

Tools:
  1. extract_selected_text  — Get selected text via clipboard trick (macOS)
  2. list_running_processes — Full process list via psutil
  3. read_sms_messages      — Read iMessage history via chat.db (macOS, strict consent)

OI method mappings per oi-source-map.md:
  - extract_selected_text  → computer.os.get_selected_text() (Cmd+C clipboard trick)
  - list_running_processes → computer.terminal.run("shell", "ps aux") / psutil
  - read_sms_messages      → computer.sms.get() (sqlite3 chat.db read, macOS)

Platform notes:
  - extract_selected_text: macOS initially (requires Cmd+C clipboard trick)
  - list_running_processes: all platforms
  - read_sms_messages: macOS ONLY (chat.db access, iMessage history)

PR-3 (2026-04-25): moved from extensions/oi-capability/ into
extensions/coding-harness/oi_bridge/ per docs/f7/interweaving-plan.md.
capability_claims declared on each class — F1 ConsentGate enforces at dispatch.
Tier 5 tools use PER_ACTION consent (niche, high-sensitivity data).
SANDBOX_HOOK: same situation as Tier 4 — pending 3.E wrapper API match.
AUDIT_HOOK markers removed: audit happens automatically through the gate (PRs #64/#65).
"""

from __future__ import annotations

from typing import Any, ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

from ..subprocess.wrapper import OISubprocessWrapper


class ExtractSelectedTextTool(BaseTool):
    """Extract currently selected text via clipboard trick (Cmd+C then read)."""

    consent_tier: int = 5
    parallel_safe: bool = False  # modifies clipboard
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="oi_bridge.extract_selected_text",
            tier_required=ConsentTier.PER_ACTION,
            human_description="Extract currently selected text via clipboard trick (briefly overwrites clipboard).",
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
            name="extract_selected_text",
            description=(
                "Extract the text currently selected in any application. "
                "Uses the Cmd+C clipboard trick — briefly overwrites clipboard. "
                "Requires per-action consent. Platform: macOS initially."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # F1 ConsentGate enforces capability_claims at dispatch (PER_ACTION tier).
        # SANDBOX_HOOK pending 3.E API match — see tier_4_system_control.py comment.
        # TODO: wire run_sandboxed once wrapper exposes pre-exec hooks.

        try:
            result = await self._wrapper.call("computer.os.get_selected_text", {})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        return ToolResult(tool_call_id=call.id, content=str(result))


class ListRunningProcessesTool(BaseTool):
    """List currently running processes (all platforms via psutil / ps aux)."""

    consent_tier: int = 5
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="oi_bridge.list_running_processes",
            tier_required=ConsentTier.PER_ACTION,
            human_description="List currently running processes (read-only, no kill/signal).",
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
            name="list_running_processes",
            description=(
                "List currently running processes with their names and PIDs. "
                "Read-only — no kill/signal permissions. Platform: all."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "Filter results by process name (optional)"},
                    "limit": {"type": "integer", "description": "Max processes to return (default: 50)", "default": 50},
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # F1 ConsentGate enforces capability_claims at dispatch (PER_ACTION tier).
        # SANDBOX_HOOK pending 3.E API match — see tier_4_system_control.py comment.
        # TODO: wire run_sandboxed once wrapper exposes pre-exec hooks.

        limit = call.arguments.get("limit", 50)
        filter_str = call.arguments.get("filter", "")

        if filter_str:
            cmd = f"ps aux | grep -i '{filter_str}' | grep -v grep | head -{limit}"
        else:
            cmd = f"ps aux | head -{limit + 1}"  # +1 for header

        try:
            result = await self._wrapper.call(
                "computer.terminal.run",
                {"language": "shell", "code": cmd},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        return ToolResult(tool_call_id=call.id, content=str(result))


class ReadSmsMessagesTool(BaseTool):
    """Read iMessage history from chat.db. Strict consent — entire history is sensitive."""

    consent_tier: int = 5
    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="oi_bridge.read_sms_messages",
            tier_required=ConsentTier.PER_ACTION,
            human_description="Read iMessage/SMS messages from macOS chat.db (entire iMessage history is sensitive).",
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
            name="read_sms_messages",
            description=(
                "Read iMessage and SMS messages from macOS chat.db. "
                "STRICT CONSENT required — entire iMessage history is sensitive. "
                "Platform: macOS ONLY."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "contact": {"type": "string", "description": "Contact name or phone number to filter by (optional)"},
                    "limit": {"type": "integer", "description": "Number of recent messages to return (default: 20)", "default": 20},
                    "substring": {"type": "string", "description": "Filter messages containing this text (optional)"},
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # F1 ConsentGate enforces capability_claims at dispatch (PER_ACTION tier).
        # SANDBOX_HOOK pending 3.E API match — see tier_4_system_control.py comment.
        # TODO: wire run_sandboxed once wrapper exposes pre-exec hooks.

        params: dict[str, Any] = {}
        if "contact" in call.arguments:
            params["contact"] = call.arguments["contact"]
        if "limit" in call.arguments:
            params["limit"] = call.arguments["limit"]
        if "substring" in call.arguments:
            params["substring"] = call.arguments["substring"]

        try:
            result = await self._wrapper.call("computer.sms.get", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        return ToolResult(tool_call_id=call.id, content=str(result))


ALL_TOOLS = [
    ExtractSelectedTextTool,
    ListRunningProcessesTool,
    ReadSmsMessagesTool,
]

__all__ = [
    "ExtractSelectedTextTool",
    "ListRunningProcessesTool",
    "ReadSmsMessagesTool",
    "ALL_TOOLS",
]
