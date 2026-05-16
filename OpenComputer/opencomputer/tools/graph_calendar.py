"""``GraphListCalendarTool`` — list calendar events through Microsoft Graph.

Build-chunk 3 of Milestone 3. The agent-facing tool over
:meth:`opencomputer.integrations.graph.client._CalendarOperations.list`
(``GET /me/calendarView``).

``calendarView`` (not ``/me/events``) is used so recurring series are expanded
into one entry per occurrence in the window — that is what a human means by
"list my calendar."

Consent tier — ``EXPLICIT``
---------------------------
This tool **reads cloud data** — the user's Outlook calendar. ``IMPLICIT``
(tier 0) is defined as "no external data read" and is therefore wrong here. The
correct tier is ``EXPLICIT``: reading the calendar is a revocable, source-level
capability the user grants once. (The OAuth consent at ``oc auth login graph``
is itself the source-level checkpoint; ``EXPLICIT`` keeps the consent gate
honest that cloud data is being read, without a per-call prompt.)

Time window
-----------
``start`` / ``end`` are optional ISO-8601 datetimes. When omitted, a sensible
default window — **now → now + 7 days** — is used. A value with no timezone
offset is treated as **UTC** (both by this tool's normalization and by Graph
itself); a value *with* an offset is honored as-is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from opencomputer.tools._graph_common import (
    NOT_AUTHENTICATED_MESSAGE,
    error_result,
    run_read_with_401_retry,
    tool_available,
)
from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

#: Default look-ahead window (days) when the caller gives no ``end``.
_DEFAULT_WINDOW_DAYS = 7

#: Hard cap on the look-back / look-ahead span (days). A window wider than this
#: would pull an unreasonable number of events into the agent's context; the
#: caller is told to narrow it rather than silently truncating.
_MAX_WINDOW_DAYS = 366

#: Cap on the number of events returned across all pages — passed to the
#: client's paginator so a packed calendar can't run unbounded.
_MAX_EVENTS = 200


def _parse_iso_datetime(field_name: str, raw: str) -> datetime:
    """Parse a caller-supplied ISO-8601 datetime, defaulting a naive value to UTC.

    Args:
        field_name: ``"start"`` / ``"end"`` — for error text only.
        raw: The ISO-8601 string from the model.

    Returns:
        A timezone-aware :class:`datetime` (UTC when the input carried no
        offset).

    Raises:
        ValueError: If ``raw`` is not parseable as an ISO-8601 datetime.
    """
    text = raw.strip()
    # Accept a trailing 'Z' (UTC) — datetime.fromisoformat handles it from
    # Python 3.11, but normalize defensively for older-style inputs.
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"'{field_name}' is not a valid ISO-8601 datetime: {raw!r}. "
            "Use a form like '2026-05-16T09:00:00Z' or "
            "'2026-05-16T09:00:00-07:00'."
        ) from exc
    if parsed.tzinfo is None:
        # A naive value is interpreted as UTC — consistent with how Graph
        # treats an offset-less startDateTime/endDateTime.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _to_graph_datetime(value: datetime) -> str:
    """Render a timezone-aware datetime as the ISO-8601 string Graph expects."""
    return value.isoformat()


def _resolve_window(
    start_raw: Any, end_raw: Any
) -> tuple[str, str]:
    """Resolve the ``(startDateTime, endDateTime)`` pair for ``calendarView``.

    Applies the now → now+7d default for missing values, validates ordering,
    and caps the span.

    Raises:
        ValueError: On an unparseable datetime, an end-before-start window, or
            a span wider than :data:`_MAX_WINDOW_DAYS`.
    """
    now = datetime.now(UTC)

    if start_raw is None or (isinstance(start_raw, str) and not start_raw.strip()):
        start = now
    elif isinstance(start_raw, str):
        start = _parse_iso_datetime("start", start_raw)
    else:
        raise ValueError(
            f"'start' must be an ISO-8601 datetime string, "
            f"got {type(start_raw).__name__}"
        )

    if end_raw is None or (isinstance(end_raw, str) and not end_raw.strip()):
        end = start + timedelta(days=_DEFAULT_WINDOW_DAYS)
    elif isinstance(end_raw, str):
        end = _parse_iso_datetime("end", end_raw)
    else:
        raise ValueError(
            f"'end' must be an ISO-8601 datetime string, "
            f"got {type(end_raw).__name__}"
        )

    if end <= start:
        raise ValueError(
            "'end' must be after 'start'. "
            f"Got start={_to_graph_datetime(start)}, end={_to_graph_datetime(end)}."
        )
    if end - start > timedelta(days=_MAX_WINDOW_DAYS):
        raise ValueError(
            f"The requested window is too wide (limit {_MAX_WINDOW_DAYS} "
            "days). Narrow the start/end range."
        )
    return _to_graph_datetime(start), _to_graph_datetime(end)


def _format_event(event: dict[str, Any]) -> str:
    """Render one Graph ``event`` object as a readable one-or-two-line summary."""
    subject = event.get("subject") or "(no subject)"
    start = _format_event_time(event.get("start"))
    end = _format_event_time(event.get("end"))
    when = (
        f"{start} → {end}"
        if start != "(unknown)" or end != "(unknown)"
        else "(time unknown)"
    )
    if event.get("isAllDay"):
        when = f"{start} (all day)"

    line = f"- {subject}  [{when}]"
    if event.get("isCancelled"):
        line += "  (cancelled)"

    extras: list[str] = []
    location = event.get("location")
    if isinstance(location, dict):
        display = location.get("displayName")
        if isinstance(display, str) and display.strip():
            extras.append(f"location: {display.strip()}")
    organizer = event.get("organizer")
    if isinstance(organizer, dict):
        email = organizer.get("emailAddress")
        if isinstance(email, dict):
            name = email.get("name") or email.get("address")
            if isinstance(name, str) and name.strip():
                extras.append(f"organizer: {name.strip()}")
    if extras:
        line += "\n    " + "  |  ".join(extras)
    return line


def _format_event_time(value: Any) -> str:
    """Render a Graph ``dateTimeTimeZone`` object (``{dateTime, timeZone}``)."""
    if not isinstance(value, dict):
        return "(unknown)"
    date_time = value.get("dateTime")
    if not isinstance(date_time, str) or not date_time:
        return "(unknown)"
    tz = value.get("timeZone")
    return f"{date_time} {tz}" if isinstance(tz, str) and tz else date_time


class GraphListCalendarTool(BaseTool):
    """List events on the signed-in Microsoft account's calendar."""

    # A read with no side effects — safe to run alongside other parallel tools.
    parallel_safe: bool = True

    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="graph.calendar.read",
            tier_required=ConsentTier.EXPLICIT,
            human_description=(
                "Read events from your Microsoft (Outlook) calendar."
            ),
            data_scope="microsoft-graph:Calendars.Read",
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="GraphListCalendar",
            description=(
                "List calendar events from the user's connected Microsoft "
                "account via Microsoft Graph (GET /me/calendarView, which "
                "expands recurring events). Requires the user to have run "
                "`oc auth login graph`. Provide an ISO-8601 start/end window; "
                "if omitted, the next 7 days from now are listed. Datetimes "
                "without a timezone offset are treated as UTC."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": (
                            "Window start as an ISO-8601 datetime, e.g. "
                            "'2026-05-16T00:00:00Z'. Defaults to the current "
                            "time if omitted."
                        ),
                    },
                    "end": {
                        "type": "string",
                        "description": (
                            "Window end as an ISO-8601 datetime. Defaults to "
                            "7 days after the start if omitted."
                        ),
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:  # noqa: D102
        if not tool_available():
            return ToolResult(
                tool_call_id=call.id,
                content=NOT_AUTHENTICATED_MESSAGE,
                is_error=True,
            )

        args = call.arguments if isinstance(call.arguments, dict) else {}
        try:
            start_dt, end_dt = _resolve_window(args.get("start"), args.get("end"))
        except ValueError as exc:
            return ToolResult(
                tool_call_id=call.id, content=str(exc), is_error=True
            )

        # Token acquisition + the 401→force-refresh→retry-once policy live in
        # run_read_with_401_retry. A read is safe to retry; a send is not.
        try:
            events = await run_read_with_401_retry(
                lambda client: client.calendar.list(
                    start_date_time=start_dt,
                    end_date_time=end_dt,
                    max_items=_MAX_EVENTS,
                )
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a clean ToolResult
            return error_result(call, exc)

        return ToolResult(
            tool_call_id=call.id,
            content=self._format_result(events, start_dt, end_dt),
        )

    @staticmethod
    def _format_result(
        events: list[dict[str, Any]], start_dt: str, end_dt: str
    ) -> str:
        """Render the event list into the tool's readable text result."""
        header = f"Calendar events from {start_dt} to {end_dt}:"
        if not events:
            return f"{header}\n(no events in this window)"
        lines = [f"{header}  ({len(events)} event(s))", ""]
        lines.extend(_format_event(event) for event in events)
        if len(events) >= _MAX_EVENTS:
            lines.append("")
            lines.append(
                f"(result capped at {_MAX_EVENTS} events — narrow the window "
                "to see more)"
            )
        return "\n".join(lines)


__all__ = ["GraphListCalendarTool"]
