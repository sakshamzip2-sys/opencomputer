"""Tests for use_cases.dev_flow_assistant.

Covers:
- morning_standup composes exactly 3 tool calls (git log, recent files, email)
- morning_standup returns expected shape
- eod_summary composes git log + calendar calls
- eod_summary returns expected shape
- detect_focus_distractions threshold logic (distracted when > threshold_apps)
- detect_focus_distractions returns correct shape
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.oi_capability.use_cases.dev_flow_assistant import (
    detect_focus_distractions,
    eod_summary,
    morning_standup,
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


# ---------------------------------------------------------------------------
# morning_standup
# ---------------------------------------------------------------------------

class TestMorningStandup:
    async def test_calls_three_tools(self):
        """morning_standup must call ReadGitLogTool, ListRecentFilesTool, and ReadEmailMetadataTool."""
        git_mock = AsyncMock(return_value=_tool_result("abc123 feat: something"))
        files_mock = AsyncMock(return_value=_tool_result("/src/main.py\n"))
        email_mock = AsyncMock(return_value=_tool_result("[{'from': 'boss@corp.com', 'subject': 'Meeting'}]"))

        with (
            patch(
                "extensions.oi_capability.tools.tier_1_introspection.ReadGitLogTool.execute",
                new=git_mock,
            ),
            patch(
                "extensions.oi_capability.tools.tier_1_introspection.ListRecentFilesTool.execute",
                new=files_mock,
            ),
            patch(
                "extensions.oi_capability.tools.tier_2_communication.ReadEmailMetadataTool.execute",
                new=email_mock,
            ),
        ):
            result = await morning_standup(_make_wrapper())

        git_mock.assert_awaited_once()
        files_mock.assert_awaited_once()
        email_mock.assert_awaited_once()

    async def test_returns_expected_shape(self):
        with (
            patch(
                "extensions.oi_capability.tools.tier_1_introspection.ReadGitLogTool.execute",
                new=AsyncMock(return_value=_tool_result("abc123")),
            ),
            patch(
                "extensions.oi_capability.tools.tier_1_introspection.ListRecentFilesTool.execute",
                new=AsyncMock(return_value=_tool_result("/src/a.py\n")),
            ),
            patch(
                "extensions.oi_capability.tools.tier_2_communication.ReadEmailMetadataTool.execute",
                new=AsyncMock(return_value=_tool_result("[]")),
            ),
        ):
            result = await morning_standup(_make_wrapper())

        assert "recent_commits" in result
        assert "modified_files" in result
        assert "unread_emails" in result
        assert "errors" in result
        assert isinstance(result["errors"], list)

    async def test_collects_errors_gracefully(self):
        """Errors from individual tools should be collected, not raised."""
        with (
            patch(
                "extensions.oi_capability.tools.tier_1_introspection.ReadGitLogTool.execute",
                new=AsyncMock(return_value=_tool_result("git error", is_error=True)),
            ),
            patch(
                "extensions.oi_capability.tools.tier_1_introspection.ListRecentFilesTool.execute",
                new=AsyncMock(return_value=_tool_result("/src/a.py")),
            ),
            patch(
                "extensions.oi_capability.tools.tier_2_communication.ReadEmailMetadataTool.execute",
                new=AsyncMock(return_value=_tool_result("[]")),
            ),
        ):
            result = await morning_standup(_make_wrapper())

        assert len(result["errors"]) >= 1


# ---------------------------------------------------------------------------
# eod_summary
# ---------------------------------------------------------------------------

class TestEodSummary:
    async def test_calls_git_and_calendar(self):
        git_mock = AsyncMock(return_value=_tool_result("commits today"))
        cal_mock = AsyncMock(return_value=_tool_result("[]"))

        with (
            patch(
                "extensions.oi_capability.tools.tier_1_introspection.ReadGitLogTool.execute",
                new=git_mock,
            ),
            patch(
                "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
                new=cal_mock,
            ),
        ):
            result = await eod_summary(_make_wrapper())

        git_mock.assert_awaited_once()
        cal_mock.assert_awaited_once()

    async def test_returns_expected_shape(self):
        with (
            patch(
                "extensions.oi_capability.tools.tier_1_introspection.ReadGitLogTool.execute",
                new=AsyncMock(return_value=_tool_result("commits")),
            ),
            patch(
                "extensions.oi_capability.tools.tier_2_communication.ListCalendarEventsTool.execute",
                new=AsyncMock(return_value=_tool_result("[]")),
            ),
        ):
            result = await eod_summary(_make_wrapper())

        assert "todays_commits" in result
        assert "tomorrows_events" in result
        assert "errors" in result


# ---------------------------------------------------------------------------
# detect_focus_distractions
# ---------------------------------------------------------------------------

_PS_AUX_MANY_APPS = """\
USER       PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND
user       100  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/chrome
user       101  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/slack
user       102  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/spotify
user       103  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/zoom
user       104  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/terminal
user       105  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/vscode
user       106  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/twitter
"""

_PS_AUX_FEW_APPS = """\
USER       PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND
user       100  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/python3
user       101  0.0  0.0  12345   123 ?        S    10:00   0:00 /usr/bin/bash
"""


class TestDetectFocusDistractions:
    async def test_distracted_when_above_threshold(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ListAppUsageTool.execute",
            new=AsyncMock(return_value=_tool_result(_PS_AUX_MANY_APPS)),
        ):
            result = await detect_focus_distractions(_make_wrapper(), threshold_apps=5)

        assert result["is_distracted"] is True
        assert result["app_switches"] > 5

    async def test_not_distracted_when_below_threshold(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ListAppUsageTool.execute",
            new=AsyncMock(return_value=_tool_result(_PS_AUX_FEW_APPS)),
        ):
            result = await detect_focus_distractions(_make_wrapper(), threshold_apps=5)

        assert result["is_distracted"] is False
        assert result["app_switches"] <= 5

    async def test_returns_correct_shape(self):
        with patch(
            "extensions.oi_capability.tools.tier_1_introspection.ListAppUsageTool.execute",
            new=AsyncMock(return_value=_tool_result(_PS_AUX_FEW_APPS)),
        ):
            result = await detect_focus_distractions(_make_wrapper())

        assert "app_switches" in result
        assert "is_distracted" in result
        assert "top_apps" in result
        assert isinstance(result["top_apps"], list)
