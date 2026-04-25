# ruff: noqa: N999  # directory name 'oi-capability' has a hyphen (required by plugin manifest)
"""Development workflow helpers.

Composes Tier 1 and Tier 2 tools to support daily dev rituals: standup
summaries, end-of-day wrap-ups, and focus-distraction detection.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from ..tools.tier_1_introspection import ListAppUsageTool, ListRecentFilesTool, ReadGitLogTool
from ..tools.tier_2_communication import ListCalendarEventsTool, ReadEmailMetadataTool

if TYPE_CHECKING:
    from ..subprocess.wrapper import OISubprocessWrapper


async def morning_standup(wrapper: OISubprocessWrapper) -> dict:
    """Gather context for a morning standup.

    Calls three tools in sequence:

    1. :class:`ReadGitLogTool` — commits from the last 24 h
    2. :class:`ListRecentFilesTool` — files modified in the last 24 h
    3. :class:`ReadEmailMetadataTool` — unread email metadata

    Returns::

        {
            "recent_commits": str,        # raw git log output
            "modified_files": str,        # raw file list output
            "unread_emails": str,         # raw email metadata output
            "errors": [str, ...],         # any per-tool error messages
        }
    """
    from plugin_sdk.core import ToolCall

    errors: list[str] = []

    # 1. Git log — last 24 h
    git_tool = ReadGitLogTool(wrapper=wrapper)
    git_call = ToolCall(
        id="standup-git-log",
        name="read_git_log",
        arguments={"limit": 50, "format": "oneline"},
    )
    git_result = await git_tool.execute(git_call)
    if git_result.is_error:
        errors.append(f"git_log: {git_result.content}")
    recent_commits = git_result.content if not git_result.is_error else ""

    # 2. Recent files — last 24 h
    files_tool = ListRecentFilesTool(wrapper=wrapper)
    files_call = ToolCall(
        id="standup-recent-files",
        name="list_recent_files",
        arguments={"hours": 24, "limit": 50},
    )
    files_result = await files_tool.execute(files_call)
    if files_result.is_error:
        errors.append(f"list_recent_files: {files_result.content}")
    modified_files = files_result.content if not files_result.is_error else ""

    # 3. Unread emails
    email_tool = ReadEmailMetadataTool(wrapper=wrapper)
    email_call = ToolCall(
        id="standup-emails",
        name="read_email_metadata",
        arguments={"number": 20, "unread_only": True},
    )
    email_result = await email_tool.execute(email_call)
    if email_result.is_error:
        errors.append(f"read_email_metadata: {email_result.content}")
    unread_emails = email_result.content if not email_result.is_error else ""

    return {
        "recent_commits": recent_commits,
        "modified_files": modified_files,
        "unread_emails": unread_emails,
        "errors": errors,
    }


async def eod_summary(wrapper: OISubprocessWrapper) -> dict:
    """Gather end-of-day summary context.

    Calls two tools:

    1. :class:`ReadGitLogTool` — today's commits
    2. :class:`ListCalendarEventsTool` — tomorrow's events (for prep)

    Returns::

        {
            "todays_commits": str,         # raw git log output
            "tomorrows_events": str,       # raw calendar events output
            "errors": [str, ...],
        }
    """
    from datetime import datetime, timedelta

    from plugin_sdk.core import ToolCall

    errors: list[str] = []
    tomorrow = (datetime.now(tz=UTC) + timedelta(days=1)).strftime("%Y-%m-%d")

    # 1. Git log — today's commits
    git_tool = ReadGitLogTool(wrapper=wrapper)
    git_call = ToolCall(
        id="eod-git-log",
        name="read_git_log",
        arguments={"limit": 30, "format": "oneline"},
    )
    git_result = await git_tool.execute(git_call)
    if git_result.is_error:
        errors.append(f"git_log: {git_result.content}")
    todays_commits = git_result.content if not git_result.is_error else ""

    # 2. Tomorrow's calendar events
    cal_tool = ListCalendarEventsTool(wrapper=wrapper)
    cal_call = ToolCall(
        id="eod-calendar",
        name="list_calendar_events",
        arguments={"start_date": tomorrow, "end_date": tomorrow},
    )
    cal_result = await cal_tool.execute(cal_call)
    if cal_result.is_error:
        errors.append(f"list_calendar_events: {cal_result.content}")
    tomorrows_events = cal_result.content if not cal_result.is_error else ""

    return {
        "todays_commits": todays_commits,
        "tomorrows_events": tomorrows_events,
        "errors": errors,
    }


async def detect_focus_distractions(
    wrapper: OISubprocessWrapper,
    *,
    threshold_apps: int = 5,
) -> dict:
    """Detect context switching / focus loss using app-usage data.

    Uses :class:`ListAppUsageTool` (Tier 1) to count distinct app names active
    in the current session. If the count exceeds *threshold_apps*, the session
    is flagged as distracted.

    Returns::

        {
            "app_switches": int,       # number of distinct apps seen
            "is_distracted": bool,     # True if app_switches > threshold_apps
            "top_apps": [str, ...],    # list of app names (most recent first)
        }
    """
    from plugin_sdk.core import ToolCall

    tool = ListAppUsageTool(wrapper=wrapper)
    call = ToolCall(
        id="detect-distractions",
        name="list_app_usage",
        arguments={"hours": 4},
    )
    result = await tool.execute(call)

    if result.is_error or not result.content.strip():
        return {"app_switches": 0, "is_distracted": False, "top_apps": []}

    # Parse ps aux output — extract COMMAND column (last field)
    app_names: list[str] = []
    seen: set[str] = set()
    for line in result.content.strip().splitlines():
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        command = parts[10]
        # Basename of command executable
        name = command.split("/")[-1].split()[0] if command else ""
        if name and name not in seen:
            seen.add(name)
            app_names.append(name)

    switches = len(app_names)
    return {
        "app_switches": switches,
        "is_distracted": switches > threshold_apps,
        "top_apps": app_names,
    }
