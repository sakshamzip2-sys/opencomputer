"""Layered Awareness MVP — Layer 2 calendar reader tests.

Mocks ``_import_event_kit`` so tests run identically on macOS, Linux, and
Windows. Real PyObjC integration is exercised manually on macOS during
the Task 13 smoke test.
"""
from unittest.mock import MagicMock, patch

from opencomputer.profile_bootstrap.calendar_reader import (
    CalendarEventSummary,
    read_upcoming_events,
)


def test_calendar_event_summary_defaults():
    e = CalendarEventSummary()
    assert e.title == ""
    assert e.location == ""


def test_read_upcoming_events_returns_empty_on_pyobjc_missing():
    with patch(
        "opencomputer.profile_bootstrap.calendar_reader._import_event_kit",
        side_effect=ImportError(),
    ):
        events = read_upcoming_events(days=7)
    assert events == []


def test_read_upcoming_events_returns_empty_when_access_denied():
    fake_ek = MagicMock()
    # Status 2 = Denied. Not in _AUTHORIZED_STATUSES (3, 4, 5) so
    # the reader must short-circuit to [].
    fake_ek.EKEventStore.alloc.return_value.init.return_value.\
        authorizationStatusForEntityType_.return_value = 2
    with patch(
        "opencomputer.profile_bootstrap.calendar_reader._import_event_kit",
        return_value=fake_ek,
    ):
        events = read_upcoming_events(days=7)
    assert events == []
