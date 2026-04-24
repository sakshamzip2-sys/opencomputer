"""Tier 2 — Communication tools (5 tools, reads + drafts).

Tools:
  1. read_email_metadata  — Email headers only (from/subject/date); no body
  2. read_email_bodies    — Full email body text (stricter consent)
  3. list_calendar_events — Calendar events via EventKit / AppleScript
  4. read_contacts        — Contacts.app via AppleScript
  5. send_email           — DRAFTS-ONLY; rejects if send_now=True

OI method mappings per oi-source-map.md:
  - read_email_metadata  → computer.mail.get() (metadata slice)
  - read_email_bodies    → computer.mail.get() (full content)
  - list_calendar_events → computer.calendar.get_events()
  - read_contacts        → computer.contacts.get_contact_info()
  - send_email           → computer.mail.send() (DRAFT MODE ONLY)

Platform notes: Mail, Calendar, Contacts are macOS ONLY (AppleScript).
"""

from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

from ..subprocess.wrapper import OISubprocessWrapper


class ReadEmailMetadataTool(BaseTool):
    """Read email metadata (from, subject, date) — no body content."""

    consent_tier: int = 2
    parallel_safe: bool = True

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
            name="read_email_metadata",
            description=(
                "Read email metadata (sender, subject, date) from the last N emails. "
                "Does NOT include email body. Platform: macOS only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "number": {"type": "integer", "description": "Number of recent emails to fetch (default: 10)", "default": 10},
                    "unread_only": {"type": "boolean", "description": "Return only unread emails (default: false)", "default": False},
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5
        # if self._consent_gate: await self._consent_gate.require(scope="oi.tier2.read_email_metadata", ...)

        params = {
            "number": call.arguments.get("number", 10),
            "unread": call.arguments.get("unread_only", False),
        }

        try:
            result = await self._wrapper.call("computer.mail.get", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # Strip body from results — metadata only
        if isinstance(result, list):
            metadata = [
                {k: v for k, v in email.items() if k in ("from", "subject", "date", "id")}
                if isinstance(email, dict)
                else email
                for email in result
            ]
            content = str(metadata)
        else:
            content = str(result)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=content)


class ReadEmailBodiesTool(BaseTool):
    """Read full email body text — stricter consent than metadata."""

    consent_tier: int = 2
    parallel_safe: bool = False  # sequential for consent tracking

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
            name="read_email_bodies",
            description=(
                "Read full email body text from the last N emails. "
                "Includes message body — more sensitive than metadata. "
                "Platform: macOS only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "number": {"type": "integer", "description": "Number of recent emails to fetch (default: 5)", "default": 5},
                    "unread_only": {"type": "boolean", "description": "Return only unread emails (default: false)", "default": False},
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5
        # Stricter than metadata: scope="oi.tier2.read_email_bodies"

        params = {
            "number": call.arguments.get("number", 5),
            "unread": call.arguments.get("unread_only", False),
        }

        try:
            result = await self._wrapper.call("computer.mail.get", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=str(result))


class ListCalendarEventsTool(BaseTool):
    """List calendar events in a date range via EventKit / AppleScript."""

    consent_tier: int = 2
    parallel_safe: bool = True

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
            name="list_calendar_events",
            description=(
                "List calendar events between start_date and end_date. "
                "Uses EventKit via AppleScript. Platform: macOS only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format (default: today)"},
                    "end_date": {"type": "string", "description": "End date in YYYY-MM-DD format (default: 7 days from start)"},
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        params: dict[str, Any] = {}
        if "start_date" in call.arguments:
            params["start_date"] = call.arguments["start_date"]
        if "end_date" in call.arguments:
            params["end_date"] = call.arguments["end_date"]

        try:
            result = await self._wrapper.call("computer.calendar.get_events", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=str(result))


class ReadContactsTool(BaseTool):
    """Read contact information from Contacts.app via AppleScript."""

    consent_tier: int = 2
    parallel_safe: bool = True

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
            name="read_contacts",
            description=(
                "Look up contact information (name, email, phone) by contact name. "
                "Reads from Contacts.app via AppleScript. Platform: macOS only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "Contact name to search for"},
                },
                "required": ["contact_name"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        try:
            result = await self._wrapper.call(
                "computer.contacts.get_contact_info",
                {"contact_name": call.arguments["contact_name"]},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=str(result))


class SendEmailTool(BaseTool):
    """Save an email DRAFT — NEVER auto-sends. Rejects if send_now=True.

    This tool enforces drafts-only policy at the wrapper level.
    Even if the user passes send_now=True, the call is rejected with ValueError.
    Actual sending requires the user to manually open Mail.app and send the draft.
    """

    consent_tier: int = 2
    parallel_safe: bool = False  # email drafting is always sequential

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
            name="send_email",
            description=(
                "Save an email as a DRAFT in Mail.app. "
                "NEVER auto-sends — the draft is saved for user review. "
                "Pass send_now=true to attempt send (will be rejected with an error). "
                "Platform: macOS only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {"type": "string", "description": "Email body text"},
                    "send_now": {
                        "type": "boolean",
                        "description": "ALWAYS rejected — tool only saves drafts. Included for clarity.",
                        "default": False,
                    },
                },
                "required": ["to", "subject", "body"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # Drafts-only enforcement — CRITICAL: never remove this check
        if call.arguments.get("send_now", False):
            raise ValueError(
                "send_email tool is DRAFTS-ONLY. "
                "Setting send_now=True is explicitly forbidden. "
                "The draft will be saved to Mail.app for manual review and sending."
            )

        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        params = {
            "to": call.arguments["to"],
            "subject": call.arguments["subject"],
            "body": call.arguments["body"],
        }

        try:
            result = await self._wrapper.call("computer.mail.send", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(
            tool_call_id=call.id,
            content=f"Draft saved: {result}",
        )


ALL_TOOLS = [
    ReadEmailMetadataTool,
    ReadEmailBodiesTool,
    ListCalendarEventsTool,
    ReadContactsTool,
    SendEmailTool,
]

__all__ = [
    "ReadEmailMetadataTool",
    "ReadEmailBodiesTool",
    "ListCalendarEventsTool",
    "ReadContactsTool",
    "SendEmailTool",
    "ALL_TOOLS",
]
