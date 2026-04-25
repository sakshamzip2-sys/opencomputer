# ruff: noqa: N999  # directory name 'oi-capability' has a hyphen (required by plugin manifest)
"""Calendar management and life-administration helpers.

Composes :class:`~..tools.tier_2_communication.ListCalendarEventsTool` into
higher-level scheduling patterns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..tools.tier_2_communication import ListCalendarEventsTool

if TYPE_CHECKING:
    from ..subprocess.wrapper import OISubprocessWrapper


def _today_str() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


def _date_plus(days: int) -> str:
    return (datetime.now(tz=UTC) + timedelta(days=days)).strftime("%Y-%m-%d")


async def upcoming_events(wrapper: OISubprocessWrapper, days_ahead: int = 7) -> list[dict]:
    """Return calendar events for the next *days_ahead* days.

    Delegates to :class:`ListCalendarEventsTool` with today as start and
    today+*days_ahead* as end.
    """
    from plugin_sdk.core import ToolCall

    tool = ListCalendarEventsTool(wrapper=wrapper)
    call = ToolCall(
        id="upcoming-events",
        name="list_calendar_events",
        arguments={
            "start_date": _today_str(),
            "end_date": _date_plus(days_ahead),
        },
    )
    result = await tool.execute(call)

    if result.is_error or not result.content.strip():
        return []

    # Parse the raw content — tool returns a string representation
    raw = result.content.strip()
    try:
        import ast

        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            return [e if isinstance(e, dict) else {"raw": str(e)} for e in parsed]
        if isinstance(parsed, dict):
            return [parsed]
    except (ValueError, SyntaxError):
        pass

    # Fallback: wrap raw string as a single item
    return [{"raw": raw}] if raw else []


async def todays_schedule(wrapper: OISubprocessWrapper) -> list[dict]:
    """Return today's calendar events only (convenience wrapper)."""
    from plugin_sdk.core import ToolCall

    tool = ListCalendarEventsTool(wrapper=wrapper)
    today = _today_str()
    call = ToolCall(
        id="todays-schedule",
        name="list_calendar_events",
        arguments={"start_date": today, "end_date": today},
    )
    result = await tool.execute(call)

    if result.is_error or not result.content.strip():
        return []

    raw = result.content.strip()
    try:
        import ast

        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            return [e if isinstance(e, dict) else {"raw": str(e)} for e in parsed]
        if isinstance(parsed, dict):
            return [parsed]
    except (ValueError, SyntaxError):
        pass

    return [{"raw": raw}] if raw else []


async def find_free_slots(
    wrapper: OISubprocessWrapper,
    *,
    duration_minutes: int = 30,
    days_ahead: int = 7,
) -> list[dict]:
    """Find free time slots over the next *days_ahead* days.

    Fetches events via :class:`ListCalendarEventsTool`, then identifies gaps
    larger than *duration_minutes* in each day's 09:00–18:00 working window.

    Returns a list of::

        {"start": "<iso8601>", "end": "<iso8601>", "duration_minutes": int}

    If the calendar is fully booked, returns an empty list.
    """
    events = await upcoming_events(wrapper, days_ahead=days_ahead)

    now = datetime.now(tz=UTC)
    free_slots: list[dict] = []

    for day_offset in range(days_ahead):
        day = now + timedelta(days=day_offset)
        # Working window: 09:00 – 18:00
        window_start = day.replace(hour=9, minute=0, second=0, microsecond=0)
        window_end = day.replace(hour=18, minute=0, second=0, microsecond=0)
        day_str = day.strftime("%Y-%m-%d")

        # Collect busy intervals from events on this day
        busy: list[tuple[datetime, datetime]] = []
        for event in events:
            event_start = event.get("start") or event.get("date") or ""
            event_end = event.get("end") or ""
            if day_str not in str(event_start) and day_str not in str(event_end):
                continue
            try:
                es = _parse_dt(str(event_start), day, window_start)
                ee = _parse_dt(str(event_end), day, window_end)
                busy.append((max(es, window_start), min(ee, window_end)))
            except (ValueError, TypeError):
                continue

        # Sort and merge overlapping busy intervals
        busy.sort(key=lambda t: t[0])
        merged: list[tuple[datetime, datetime]] = []
        for start, end in busy:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))

        # Find free gaps
        cursor = window_start
        for busy_start, busy_end in merged:
            if cursor < busy_start:
                gap_minutes = int((busy_start - cursor).total_seconds() / 60)
                if gap_minutes >= duration_minutes:
                    free_slots.append({
                        "start": cursor.isoformat(),
                        "end": busy_start.isoformat(),
                        "duration_minutes": gap_minutes,
                    })
            cursor = max(cursor, busy_end)

        # Tail gap after last event
        if cursor < window_end:
            gap_minutes = int((window_end - cursor).total_seconds() / 60)
            if gap_minutes >= duration_minutes:
                free_slots.append({
                    "start": cursor.isoformat(),
                    "end": window_end.isoformat(),
                    "duration_minutes": gap_minutes,
                })

    return free_slots


def _parse_dt(value: str, day: datetime, fallback: datetime) -> datetime:
    """Best-effort datetime parser for calendar event timestamps."""
    if not value or value in ("None", "null", ""):
        return fallback
    # Try ISO 8601 with timezone
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(value[:19], fmt[:len(fmt)])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            continue
    return fallback
