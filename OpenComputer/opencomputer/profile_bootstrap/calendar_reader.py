"""Layer 2 helper — read upcoming calendar events via PyObjC EventKit.

Returns ``[]`` when:
- Not on macOS (PyObjC import fails)
- User has not granted Calendar access in System Settings
- EventKit authorization status is anything other than ``Authorized``

The CLI-level consent gate (``ingestion.calendar``, EXPLICIT) is the
*authorization* layer. The macOS Privacy & Security pane is a
separate, OS-level grant that we cannot bypass.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("opencomputer.profile_bootstrap.calendar")


@dataclass(frozen=True, slots=True)
class CalendarEventSummary:
    """One calendar event, summary only — no attendee emails."""

    title: str = ""
    start: float = 0.0  # epoch seconds
    end: float = 0.0
    location: str = ""
    calendar_name: str = ""


#: Authorized statuses for EKEntityType.event. We accept any status
#: that lets us read events. Numeric values match Apple's
#: ``EKAuthorizationStatus`` enum (1=Restricted, 2=Denied, 3=Authorized,
#: 4=WriteOnly, 5=FullAccess as of macOS 14+). Using a set rather than
#: a single magic number keeps the check forward-compatible.
_AUTHORIZED_STATUSES = frozenset({3, 4, 5})


def _import_event_kit() -> Any:
    """Indirect import so tests can patch easily."""
    import EventKit  # type: ignore[import-not-found]
    return EventKit


def read_upcoming_events(*, days: int = 7) -> list[CalendarEventSummary]:
    """Read calendar events for the next ``days`` from macOS Calendar.

    Best-effort. Returns ``[]`` on any failure path.
    """
    try:
        ek = _import_event_kit()
    except ImportError:
        _log.debug("EventKit not importable — non-macOS or PyObjC missing")
        return []

    try:
        store = ek.EKEventStore.alloc().init()
        # EKEntityTypeEvent is integer 0 in Apple's enum. We pass it
        # directly to avoid coupling to a PyObjC-exposed constant
        # name (which has changed across PyObjC versions).
        status = store.authorizationStatusForEntityType_(0)
        if int(status) not in _AUTHORIZED_STATUSES:
            _log.info("Calendar access not granted (status=%s)", status)
            return []

        from Foundation import NSDate  # type: ignore[import-not-found]

        now = NSDate.date()
        end = NSDate.dateWithTimeIntervalSinceNow_(days * 24 * 3600)
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            now, end, None,
        )
        events = store.eventsMatchingPredicate_(predicate) or []
    except Exception as exc:  # noqa: BLE001
        _log.warning("EventKit read failed: %s", exc)
        return []

    out: list[CalendarEventSummary] = []
    for ev in events:
        try:
            out.append(
                CalendarEventSummary(
                    title=str(ev.title() or "")[:200],
                    start=float(ev.startDate().timeIntervalSince1970()),
                    end=float(ev.endDate().timeIntervalSince1970()),
                    location=str(ev.location() or "")[:200],
                    calendar_name=str(ev.calendar().title() or "")[:100],
                )
            )
        except Exception:  # noqa: BLE001
            continue
    return out
