# ruff: noqa: N999  # directory name 'oi-capability' has a hyphen (required by plugin manifest)
"""Temporal pattern recognition helpers.

Composes Tier 1 and Tier 2 tools to analyse usage heatmaps, commit cadence,
and meeting density over configurable look-back windows.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..tools.tier_1_introspection import ListAppUsageTool, ReadGitLogTool
from ..tools.tier_2_communication import ListCalendarEventsTool

if TYPE_CHECKING:
    from ..subprocess.wrapper import OISubprocessWrapper

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


async def daily_activity_heatmap(
    wrapper: OISubprocessWrapper,
    *,
    days_back: int = 30,
) -> dict[str, list[int]]:
    """Build an hour-of-day activity heatmap aggregated by weekday.

    Uses :class:`ListAppUsageTool` to sample active processes, counts
    occurrences per hour per weekday.  Since the tool returns a snapshot (not
    a full history), this function records the *current* hour as an activity
    count and fills the rest of the structure with zeros.

    For a production implementation, Session A Phase 5 wiring would feed real
    usage telemetry from the agent bus.

    Returns::

        {
            "Mon": [count_h0, count_h1, ..., count_h23],  # 24 int entries
            "Tue": [...],
            ...
            "Sun": [...],
        }
    """
    from plugin_sdk.core import ToolCall

    # Initialise heatmap
    heatmap: dict[str, list[int]] = {day: [0] * 24 for day in _WEEKDAY_NAMES}

    tool = ListAppUsageTool(wrapper=wrapper)
    call = ToolCall(
        id="heatmap-app-usage",
        name="list_app_usage",
        arguments={"hours": min(days_back * 24, 168)},  # cap at 7 days for the tool
    )
    result = await tool.execute(call)

    if result.is_error or not result.content.strip():
        return heatmap

    # Count non-empty lines as process entries — each represents a usage event.
    # Bucket them into the current weekday/hour as a simple proxy.
    now = datetime.now(tz=UTC)
    weekday_name = _WEEKDAY_NAMES[now.weekday()]
    hour = now.hour

    # Count processes seen as the activity count for this hour
    active_count = sum(
        1
        for line in result.content.strip().splitlines()
        if line.strip() and not line.startswith("USER")
    )
    heatmap[weekday_name][hour] = active_count

    return heatmap


async def commit_cadence(
    wrapper: OISubprocessWrapper,
    *,
    days_back: int = 30,
) -> dict:
    """Analyse git commit cadence over the last *days_back* days.

    Uses :class:`ReadGitLogTool` to fetch recent commits, then aggregates
    daily commit counts to compute:

    * ``daily_avg`` — average commits per calendar day
    * ``weekday_avg`` — average commits on Mon–Fri
    * ``weekend_avg`` — average commits on Sat–Sun
    * ``longest_streak`` — longest consecutive days with at least one commit

    Returns::

        {
            "daily_avg": float,
            "weekday_avg": float,
            "weekend_avg": float,
            "longest_streak": int,
        }
    """
    from plugin_sdk.core import ToolCall

    tool = ReadGitLogTool(wrapper=wrapper)
    call = ToolCall(
        id="commit-cadence-log",
        name="read_git_log",
        arguments={"limit": days_back * 20, "format": "short"},
    )
    result = await tool.execute(call)

    # Parse commit dates from "short" format output
    # short format lines look like: "commit <hash>" then blank then "Author:" then "Date:"
    daily_counts: dict[str, int] = defaultdict(int)

    if not result.is_error and result.content.strip():
        current_date: str | None = None
        for line in result.content.splitlines():
            line = line.strip()
            if line.startswith("Date:"):
                date_str = line[5:].strip()
                try:
                    # Git date format: "Mon Jan 1 12:00:00 2024 +0000"
                    dt = datetime.strptime(date_str[:24], "%a %b %d %H:%M:%S %Y")
                    current_date = dt.strftime("%Y-%m-%d")
                    daily_counts[current_date] += 1
                except ValueError:
                    pass

    # Build date range
    now = datetime.now(tz=UTC)
    date_range = [
        (now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_back)
    ]

    total_commits = sum(daily_counts.values())
    daily_avg = total_commits / days_back if days_back > 0 else 0.0

    weekday_commits: list[int] = []
    weekend_commits: list[int] = []
    for date_str in date_range:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            count = daily_counts.get(date_str, 0)
            if dt.weekday() < 5:
                weekday_commits.append(count)
            else:
                weekend_commits.append(count)
        except ValueError:
            continue

    weekday_avg = sum(weekday_commits) / len(weekday_commits) if weekday_commits else 0.0
    weekend_avg = sum(weekend_commits) / len(weekend_commits) if weekend_commits else 0.0

    # Longest consecutive commit streak
    longest_streak = 0
    current_streak = 0
    for date_str in reversed(date_range):
        if daily_counts.get(date_str, 0) > 0:
            current_streak += 1
            longest_streak = max(longest_streak, current_streak)
        else:
            current_streak = 0

    return {
        "daily_avg": round(daily_avg, 2),
        "weekday_avg": round(weekday_avg, 2),
        "weekend_avg": round(weekend_avg, 2),
        "longest_streak": longest_streak,
    }


async def meeting_density(
    wrapper: OISubprocessWrapper,
    *,
    days_back: int = 30,
) -> dict:
    """Compute meeting density metrics from calendar events.

    Uses :class:`ListCalendarEventsTool` (Tier 2) to fetch events over the
    last *days_back* days.

    Returns::

        {
            "meetings_per_week_avg": float,
            "longest_meeting_free_block_h": float,
        }
    """
    from plugin_sdk.core import ToolCall

    tool = ListCalendarEventsTool(wrapper=wrapper)
    now = datetime.now(tz=UTC)
    start_date = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    call = ToolCall(
        id="meeting-density-cal",
        name="list_calendar_events",
        arguments={"start_date": start_date, "end_date": end_date},
    )
    result = await tool.execute(call)

    if result.is_error or not result.content.strip():
        return {"meetings_per_week_avg": 0.0, "longest_meeting_free_block_h": 0.0}

    raw = result.content.strip()
    events: list[dict] = []
    try:
        import ast

        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            events = [e if isinstance(e, dict) else {} for e in parsed]
        elif isinstance(parsed, dict):
            events = [parsed]
    except (ValueError, SyntaxError):
        # Can't parse — use line count as proxy
        line_count = len([ln for ln in raw.splitlines() if ln.strip()])
        events = [{}] * line_count

    total_meetings = len(events)
    weeks = max(1, days_back / 7)
    meetings_per_week_avg = total_meetings / weeks

    # Longest meeting-free block: estimate from gaps between events
    # Without reliable datetime parsing, compute a stub based on event density
    # A full day has 8 working hours; deduct average meeting time
    avg_meeting_h = 1.0  # assume 1 h per event
    working_hours_per_day = 8.0
    meetings_per_day = total_meetings / max(1, days_back)
    free_hours_per_day = max(0.0, working_hours_per_day - meetings_per_day * avg_meeting_h)
    longest_meeting_free_block_h = free_hours_per_day

    return {
        "meetings_per_week_avg": round(meetings_per_week_avg, 2),
        "longest_meeting_free_block_h": round(longest_meeting_free_block_h, 2),
    }
