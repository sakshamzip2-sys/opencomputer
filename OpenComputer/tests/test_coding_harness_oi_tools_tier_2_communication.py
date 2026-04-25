"""Tests for Tier 2 communication tools (5 tools).

Key assertions:
- Schema correctness for all 5 tools
- consent_tier == 2
- SendEmailTool.execute() RAISES ValueError when send_now=True
- SendEmailTool.execute() saves draft (calls mail.send) when send_now=False
- ReadEmailMetadataTool strips body from results
- All tools route to correct OI methods
- Wrapper errors propagated as ToolResult(is_error=True)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from extensions.coding_harness.oi_bridge.tools.tier_2_communication import (
    ALL_TOOLS,
    ListCalendarEventsTool,
    ReadContactsTool,
    ReadEmailBodiesTool,
    ReadEmailMetadataTool,
    SendEmailTool,
)

from plugin_sdk.core import ToolCall


def _make_wrapper(result=None, raises=None):
    wrapper = MagicMock()
    if raises is not None:
        wrapper.call = AsyncMock(side_effect=raises)
    else:
        wrapper.call = AsyncMock(return_value=result if result is not None else {})
    return wrapper


def _make_call(name: str, arguments: dict) -> ToolCall:
    return ToolCall(id="t2-call", name=name, arguments=arguments)


class TestAllTier2ToolsList:
    def test_all_tools_has_5_entries(self):
        assert len(ALL_TOOLS) == 5

    def test_all_tools_have_consent_tier_2(self):
        wrapper = _make_wrapper()
        for cls in ALL_TOOLS:
            tool = cls(wrapper=wrapper)
            assert tool.consent_tier == 2, f"{cls.__name__} should have consent_tier=2"


class TestReadEmailMetadataTool:
    def test_schema_name(self):
        tool = ReadEmailMetadataTool(wrapper=_make_wrapper())
        assert tool.schema.name == "read_email_metadata"

    def test_schema_no_required_params(self):
        tool = ReadEmailMetadataTool(wrapper=_make_wrapper())
        assert tool.schema.parameters["required"] == []

    async def test_execute_calls_mail_get(self):
        wrapper = _make_wrapper(result=[{"from": "alice@example.com", "subject": "Hi", "date": "2024-01-01", "body": "SECRET"}])
        tool = ReadEmailMetadataTool(wrapper=wrapper)
        call = _make_call("read_email_metadata", {"number": 5})
        result = await tool.execute(call)
        wrapper.call.assert_awaited_once()
        method = wrapper.call.call_args[0][0]
        assert method == "computer.mail.get"
        assert not result.is_error

    async def test_execute_strips_body_from_result(self):
        """Metadata tool must NOT return email body."""
        wrapper = _make_wrapper(result=[{
            "from": "alice@example.com",
            "subject": "Hello",
            "date": "2024-01-01",
            "id": "msg-1",
            "body": "This is secret body text",
        }])
        tool = ReadEmailMetadataTool(wrapper=wrapper)
        call = _make_call("read_email_metadata", {})
        result = await tool.execute(call)
        # Body should be stripped from the content
        assert "secret body text" not in result.content

    async def test_execute_error_propagation(self):
        wrapper = _make_wrapper(raises=RuntimeError("mail error"))
        tool = ReadEmailMetadataTool(wrapper=wrapper)
        call = _make_call("read_email_metadata", {})
        result = await tool.execute(call)
        assert result.is_error


class TestReadEmailBodiesTool:
    def test_schema_name(self):
        tool = ReadEmailBodiesTool(wrapper=_make_wrapper())
        assert tool.schema.name == "read_email_bodies"

    async def test_execute_calls_mail_get(self):
        wrapper = _make_wrapper(result=[{"body": "email content"}])
        tool = ReadEmailBodiesTool(wrapper=wrapper)
        call = _make_call("read_email_bodies", {"number": 3})
        result = await tool.execute(call)
        wrapper.call.assert_awaited_once()
        assert wrapper.call.call_args[0][0] == "computer.mail.get"
        assert not result.is_error

    async def test_execute_includes_body(self):
        """Bodies tool SHOULD include body content."""
        wrapper = _make_wrapper(result=[{"body": "important email body"}])
        tool = ReadEmailBodiesTool(wrapper=wrapper)
        call = _make_call("read_email_bodies", {})
        result = await tool.execute(call)
        assert "important email body" in result.content


class TestListCalendarEventsTool:
    def test_schema_name(self):
        tool = ListCalendarEventsTool(wrapper=_make_wrapper())
        assert tool.schema.name == "list_calendar_events"

    def test_schema_no_required_params(self):
        tool = ListCalendarEventsTool(wrapper=_make_wrapper())
        assert tool.schema.parameters["required"] == []

    async def test_execute_calls_calendar_get_events(self):
        wrapper = _make_wrapper(result=[{"title": "Meeting", "date": "2024-01-10"}])
        tool = ListCalendarEventsTool(wrapper=wrapper)
        call = _make_call("list_calendar_events", {"start_date": "2024-01-01", "end_date": "2024-01-31"})
        result = await tool.execute(call)
        method = wrapper.call.call_args[0][0]
        assert method == "computer.calendar.get_events"
        assert not result.is_error

    async def test_execute_no_params_still_works(self):
        wrapper = _make_wrapper(result=[])
        tool = ListCalendarEventsTool(wrapper=wrapper)
        call = _make_call("list_calendar_events", {})
        result = await tool.execute(call)
        assert not result.is_error


class TestReadContactsTool:
    def test_schema_name(self):
        tool = ReadContactsTool(wrapper=_make_wrapper())
        assert tool.schema.name == "read_contacts"

    def test_schema_requires_contact_name(self):
        tool = ReadContactsTool(wrapper=_make_wrapper())
        assert "contact_name" in tool.schema.parameters["required"]

    async def test_execute_calls_contacts_get_contact_info(self):
        wrapper = _make_wrapper(result={"name": "Alice", "email": "alice@example.com"})
        tool = ReadContactsTool(wrapper=wrapper)
        call = _make_call("read_contacts", {"contact_name": "Alice"})
        result = await tool.execute(call)
        method = wrapper.call.call_args[0][0]
        assert method == "computer.contacts.get_contact_info"
        params = wrapper.call.call_args[0][1]
        assert params["contact_name"] == "Alice"
        assert not result.is_error


class TestSendEmailTool:
    def test_schema_name(self):
        tool = SendEmailTool(wrapper=_make_wrapper())
        assert tool.schema.name == "send_email"

    def test_schema_requires_to_subject_body(self):
        tool = SendEmailTool(wrapper=_make_wrapper())
        required = tool.schema.parameters["required"]
        assert "to" in required
        assert "subject" in required
        assert "body" in required

    async def test_execute_raises_when_send_now_true(self):
        """CRITICAL: send_now=True must ALWAYS raise ValueError."""
        wrapper = _make_wrapper()
        tool = SendEmailTool(wrapper=wrapper)
        call = _make_call("send_email", {
            "to": "bob@example.com",
            "subject": "Test",
            "body": "Hello",
            "send_now": True,
        })
        with pytest.raises(ValueError) as exc_info:
            await tool.execute(call)
        assert "draft" in str(exc_info.value).lower() or "drafts" in str(exc_info.value).lower()
        # Wrapper must NOT be called — no actual send attempted
        wrapper.call.assert_not_awaited()

    async def test_execute_saves_draft_when_send_now_false(self):
        """Default send_now=False should call mail.send (for draft save)."""
        wrapper = _make_wrapper(result={"status": "draft_saved"})
        tool = SendEmailTool(wrapper=wrapper)
        call = _make_call("send_email", {
            "to": "bob@example.com",
            "subject": "Test",
            "body": "Hello",
            "send_now": False,
        })
        result = await tool.execute(call)
        assert not result.is_error
        wrapper.call.assert_awaited_once()
        method = wrapper.call.call_args[0][0]
        assert method == "computer.mail.send"

    async def test_execute_sends_draft_when_send_now_not_provided(self):
        """If send_now is absent, default False → draft path."""
        wrapper = _make_wrapper(result={"status": "draft_saved"})
        tool = SendEmailTool(wrapper=wrapper)
        call = _make_call("send_email", {
            "to": "carol@example.com",
            "subject": "Draft email",
            "body": "Content",
        })
        result = await tool.execute(call)
        assert not result.is_error

    async def test_execute_passes_correct_params_to_mail_send(self):
        wrapper = _make_wrapper(result={})
        tool = SendEmailTool(wrapper=wrapper)
        call = _make_call("send_email", {
            "to": "dave@example.com",
            "subject": "My Subject",
            "body": "My Body",
        })
        await tool.execute(call)
        params = wrapper.call.call_args[0][1]
        assert params["to"] == "dave@example.com"
        assert params["subject"] == "My Subject"
        assert params["body"] == "My Body"

    async def test_execute_error_propagation(self):
        wrapper = _make_wrapper(raises=RuntimeError("mail app not running"))
        tool = SendEmailTool(wrapper=wrapper)
        call = _make_call("send_email", {"to": "x@x.com", "subject": "S", "body": "B"})
        result = await tool.execute(call)
        assert result.is_error
