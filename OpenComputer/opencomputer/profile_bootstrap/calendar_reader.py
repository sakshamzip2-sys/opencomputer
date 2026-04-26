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
import threading
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


def _import_foundation() -> Any:
    """Indirect import so tests can patch easily."""
    import Foundation  # type: ignore[import-not-found]
    return Foundation


def _request_calendar_access(
    store: Any, ek: Any, timeout_seconds: float = 60.0,
) -> bool:
    """Trigger macOS Calendar permission dialog and block until decided.

    Returns True if granted, False if denied or timed out.

    The 60s timeout is generous because the user has to physically click
    the dialog button — they may take their time. PyObjC dispatches the
    completion callback on EventKit's serial queue (a different thread),
    so ``threading.Event`` is the correct synchronization primitive.

    Caveat: macOS Privacy dialogs require an active CFRunLoop on the
    main thread to display. For the CLI ``opencomputer profile bootstrap``
    flow this holds (we run synchronously on the main thread). When this
    helper is called from a worker thread the dialog may not appear and
    the request silently times out; the caller treats that as "denied".
    """
    event = threading.Event()
    result = {"granted": False}

    def completion(granted: Any, error: Any) -> None:
        # PyObjC translates Objective-C bool → Python bool.
        result["granted"] = bool(granted)
        if error is not None:
            _log.warning("Calendar access request error: %s", error)
        event.set()

    # Trigger the dialog — fires the callback when the user decides.
    store.requestAccessToEntityType_completion_(0, completion)

    # Block until callback fires or timeout elapses.
    if not event.wait(timeout=timeout_seconds):
        _log.warning(
            "Calendar access request timed out after %ss", timeout_seconds,
        )
        return False

    return result["granted"]


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

        # Status 0 = NotDetermined — actively request access so the user
        # gets the macOS Privacy & Security dialog. Subsequent runs won't
        # re-prompt (macOS persists the user's decision).
        if int(status) == 0:
            granted = _request_calendar_access(store, ek)
            if not granted:
                _log.info("Calendar access denied by user")
                return []
            # Re-read status after grant — should now be Authorized.
            status = store.authorizationStatusForEntityType_(0)

        if int(status) not in _AUTHORIZED_STATUSES:
            _log.info("Calendar access not granted (status=%s)", status)
            return []

        foundation = _import_foundation()
        NSDate = foundation.NSDate

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
