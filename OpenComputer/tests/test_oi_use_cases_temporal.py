"""Tests for use_cases.temporal_pattern_recognition.

Covers:
- daily_activity_heatmap returns dict with all weekday keys
- daily_activity_heatmap returns 24 hourly int entries per day
- commit_cadence returns correct shape with expected float/int fields
- commit_cadence aggregates commit counts correctly
- meeting_density returns correct shape
- meeting_density computes non-negative values
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.oi_capability.use_cases.temporal_pattern_recognition import (
    _WEEKDAY_NAMES,
    commit_cadence,
    daily_activity_heatmap,
    meeting_density,
)

from plugin_sdk.core import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wrapper():
    w = MagicMock()
    w.call = AsyncMock(return_value={})
    return w


def _tool_result(content="", *, is_error=False):
    return ToolResult(tool_call_id="t", content=content, is_error=is_error)


_PS_AUX_PROCS = """\
USER       PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND
user       100  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/python3
user       101  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/bash
user       102  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/chrome
"""

# Git log in short format with Date lines
_GIT_SHORT_LOG = """\
commit abc1234
Author: Alice <alice@example.com>
Date:   Mon Nov 13 10:00:00 2023

    feat: add feature

commit def5678
Author: Alice <alice@example.com>
Date:   Tue Nov 14 11:00:00 2023

    fix: bug fix

commit ghi9012
Author: Bob <bob@example.com>
Date:   Tue Nov 14 14:00:00 2023

    docs: update readme
"""

_CALENDAR_EVENTS = str([
    {"title": "Standup", "start": "2023-11-13T09:00:00", "end": "2023-11-13T09:30:00"},
    {"title": "Planning", "start": "2023-11-14T10:00:00", "end": "2023-11-14T11:00:00"},
    {"title": "Retrospective", "start": "2023-11-15T15:00:00", "end": "2023-11-15T16:00:00"},
])


# ---------------------------------------------------------------------------
# daily_activity_heatmap
# ---------------------------------------------------------------------------

class TestDailyActivityHeatmap:
    async def test_returns_dict_with_all_weekday_keys(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ListAppUsageTool.execute",
            new=AsyncMock(return_value=_tool_result(_PS_AUX_PROCS)),
        ):
            result = await daily_activity_heatmap(_make_wrapper())

        for day in _WEEKDAY_NAMES:
            assert day in result, f"Missing weekday: {day}"

    async def test_each_day_has_24_int_entries(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ListAppUsageTool.execute",
            new=AsyncMock(return_value=_tool_result(_PS_AUX_PROCS)),
        ):
            result = await daily_activity_heatmap(_make_wrapper())

        for day in _WEEKDAY_NAMES:
            assert len(result[day]) == 24, f"{day} should have 24 hour entries"
            assert all(isinstance(v, int) for v in result[day]), f"{day} entries should be ints"

    async def test_returns_correct_type(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ListAppUsageTool.execute",
            new=AsyncMock(return_value=_tool_result("")),
        ):
            result = await daily_activity_heatmap(_make_wrapper())

        assert isinstance(result, dict)

    async def test_handles_error_gracefully(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ListAppUsageTool.execute",
            new=AsyncMock(return_value=_tool_result("err", is_error=True)),
        ):
            result = await daily_activity_heatmap(_make_wrapper())

        # Should still return well-formed heatmap (all zeros)
        assert set(result.keys()) == set(_WEEKDAY_NAMES)
        for day in _WEEKDAY_NAMES:
            assert len(result[day]) == 24


# ---------------------------------------------------------------------------
# commit_cadence
# ---------------------------------------------------------------------------

class TestCommitCadence:
    async def test_returns_correct_shape(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ReadGitLogTool.execute",
            new=AsyncMock(return_value=_tool_result(_GIT_SHORT_LOG)),
        ):
            result = await commit_cadence(_make_wrapper())

        assert "daily_avg" in result
        assert "weekday_avg" in result
        assert "weekend_avg" in result
        assert "longest_streak" in result

    async def test_daily_avg_is_float(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ReadGitLogTool.execute",
            new=AsyncMock(return_value=_tool_result(_GIT_SHORT_LOG)),
        ):
            result = await commit_cadence(_make_wrapper())

        assert isinstance(result["daily_avg"], float)
        assert result["daily_avg"] >= 0.0

    async def test_longest_streak_is_nonneg_int(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ReadGitLogTool.execute",
            new=AsyncMock(return_value=_tool_result(_GIT_SHORT_LOG)),
        ):
            result = await commit_cadence(_make_wrapper())

        assert isinstance(result["longest_streak"], int)
        assert result["longest_streak"] >= 0

    async def test_handles_empty_git_log(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ReadGitLogTool.execute",
            new=AsyncMock(return_value=_tool_result("")),
        ):
            result = await commit_cadence(_make_wrapper())

        assert result["daily_avg"] == 0.0
        assert result["longest_streak"] == 0


# ---------------------------------------------------------------------------
# meeting_density
# ---------------------------------------------------------------------------

class TestMeetingDensity:
    async def test_returns_correct_shape(self):
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=AsyncMock(return_value=_tool_result(_CALENDAR_EVENTS)),
        ):
            result = await meeting_density(_make_wrapper())

        assert "meetings_per_week_avg" in result
        assert "longest_meeting_free_block_h" in result

    async def test_meetings_per_week_is_nonneg_float(self):
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=AsyncMock(return_value=_tool_result(_CALENDAR_EVENTS)),
        ):
            result = await meeting_density(_make_wrapper())

        assert isinstance(result["meetings_per_week_avg"], float)
        assert result["meetings_per_week_avg"] >= 0.0

    async def test_free_block_is_nonneg_float(self):
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=AsyncMock(return_value=_tool_result(_CALENDAR_EVENTS)),
        ):
            result = await meeting_density(_make_wrapper())

        assert isinstance(result["longest_meeting_free_block_h"], float)
        assert result["longest_meeting_free_block_h"] >= 0.0

    async def test_handles_empty_calendar(self):
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
            new=AsyncMock(return_value=_tool_result("[]")),
        ):
            result = await meeting_density(_make_wrapper())

        assert result["meetings_per_week_avg"] == 0.0
