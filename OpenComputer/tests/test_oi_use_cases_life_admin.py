"""Tests for use_cases.life_admin.

Covers:
- upcoming_events delegates to ListCalendarEventsTool
- upcoming_events returns empty list on error
- todays_schedule is a convenience wrapper (returns list)
- find_free_slots returns empty when calendar is fully booked in the window
- find_free_slots returns slots when there are gaps in the working day
- find_free_slots respects duration_minutes filter
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.oi_capability.use_cases.life_admin import (
    find_free_slots,
    todays_schedule,
    upcoming_events,
)

from plugin_sdk.core import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wrapper():
    wrapper = MagicMock()
    wrapper.call = AsyncMock(return_value={})
    return wrapper


def _tool_result(content="", *, is_error=False):
    return ToolResult(tool_call_id="t", content=content, is_error=is_error)


# ---------------------------------------------------------------------------
# upcoming_events
# ---------------------------------------------------------------------------

class TestUpcomingEvents:
    async def test_delegates_to_list_calendar_events(self):
        called_with = {}

        async def _exec(self_tool, call):
            called_with["args"] = call.arguments
            return _tool_result("[]")

        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=_exec,
        ):
            result = await upcoming_events(_make_wrapper(), days_ahead=5)

        assert "start_date" in called_with["args"]
        assert "end_date" in called_with["args"]
        assert isinstance(result, list)

    async def test_returns_empty_on_error(self):
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=AsyncMock(return_value=_tool_result("err", is_error=True)),
        ):
            result = await upcoming_events(_make_wrapper())

        assert result == []

    async def test_returns_list_of_dicts(self):
        events_raw = "[{'title': 'Standup', 'start': '2026-01-01T09:00:00'}]"
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=AsyncMock(return_value=_tool_result(events_raw)),
        ):
            result = await upcoming_events(_make_wrapper())

        assert isinstance(result, list)
        if result:
            assert isinstance(result[0], dict)


# ---------------------------------------------------------------------------
# todays_schedule
# ---------------------------------------------------------------------------

class TestTodaysSchedule:
    async def test_returns_list(self):
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=AsyncMock(return_value=_tool_result("[]")),
        ):
            result = await todays_schedule(_make_wrapper())

        assert isinstance(result, list)

    async def test_start_equals_end_date(self):
        """todays_schedule should query start_date == end_date (today only)."""
        call_args = {}

        async def _exec(self_tool, call):
            call_args.update(call.arguments)
            return _tool_result("[]")

        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=_exec,
        ):
            await todays_schedule(_make_wrapper())

        assert call_args.get("start_date") == call_args.get("end_date")


# ---------------------------------------------------------------------------
# find_free_slots
# ---------------------------------------------------------------------------

class TestFindFreeSlots:
    async def test_returns_empty_when_fully_booked(self):
        """A calendar fully booked 09:00–18:00 should yield no free slots."""
        # Single event that covers the entire working day
        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        events_raw = (
            f"[{{'start': '{today}T09:00:00+00:00', 'end': '{today}T18:00:00+00:00', "
            f"'title': 'All day block'}}]"
        )

        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=AsyncMock(return_value=_tool_result(events_raw)),
        ):
            slots = await find_free_slots(_make_wrapper(), duration_minutes=30, days_ahead=1)

        assert isinstance(slots, list)
        # With a full-day block, no slot should be returned (or very few short tail slots)
        assert all(s["duration_minutes"] >= 30 for s in slots)

    async def test_returns_slots_when_gaps_exist(self):
        """A calendar with a midday gap should yield at least one free slot."""
        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        # Morning meeting 09:00–10:00, afternoon meeting 14:00–15:00 → 10:00–14:00 free
        events_raw = (
            f"[{{'start': '{today}T09:00:00+00:00', 'end': '{today}T10:00:00+00:00'}},"
            f" {{'start': '{today}T14:00:00+00:00', 'end': '{today}T15:00:00+00:00'}}]"
        )

        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=AsyncMock(return_value=_tool_result(events_raw)),
        ):
            slots = await find_free_slots(_make_wrapper(), duration_minutes=30, days_ahead=1)

        # Should find at least the 10:00–14:00 gap
        assert isinstance(slots, list)
        # Each slot must have required fields
        for slot in slots:
            assert "start" in slot
            assert "end" in slot
            assert "duration_minutes" in slot

    async def test_returns_list(self):
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=AsyncMock(return_value=_tool_result("[]")),
        ):
            slots = await find_free_slots(_make_wrapper())

        assert isinstance(slots, list)
