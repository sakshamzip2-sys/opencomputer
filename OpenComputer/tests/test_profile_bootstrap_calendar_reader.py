"""Layered Awareness MVP — Layer 2 calendar reader tests.

Mocks ``_import_event_kit`` so tests run identically on macOS, Linux, and
Windows. Real PyObjC integration is exercised manually on macOS during
the Task 13 smoke test.
"""
from unittest.mock import MagicMock, patch

from opencomputer.profile_bootstrap.calendar_reader import (
    CalendarEventSummary,
    _request_calendar_access,
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


def test_read_upcoming_events_requests_access_when_not_determined():
    """Status=0 (NotDetermined) triggers requestAccessToEntityType; granted=True proceeds."""
    fake_ek = MagicMock()
    store = fake_ek.EKEventStore.alloc.return_value.init.return_value

    # First status call returns 0 (NotDetermined); after grant, returns 3 (Authorized).
    status_sequence = iter([0, 3])
    store.authorizationStatusForEntityType_.side_effect = (
        lambda et: next(status_sequence)
    )

    # Make requestAccessToEntityType_completion_ invoke the callback synchronously
    # with granted=True.
    def fake_request(entity_type, completion):
        completion(True, None)
    store.requestAccessToEntityType_completion_.side_effect = fake_request

    # Predicate query needs to return [] for clean exit.
    store.predicateForEventsWithStartDate_endDate_calendars_.return_value = "predicate"
    store.eventsMatchingPredicate_.return_value = []

    fake_foundation = MagicMock()

    with patch(
        "opencomputer.profile_bootstrap.calendar_reader._import_event_kit",
        return_value=fake_ek,
    ), patch(
        "opencomputer.profile_bootstrap.calendar_reader._import_foundation",
        return_value=fake_foundation,
    ):
        events = read_upcoming_events(days=7)

    # Verify the dialog was triggered.
    store.requestAccessToEntityType_completion_.assert_called_once()
    # Result is empty (no events) but no early-return short-circuit.
    assert events == []


def test_read_upcoming_events_returns_empty_when_user_denies():
    """Status=0 → request → user denies → []."""
    fake_ek = MagicMock()
    store = fake_ek.EKEventStore.alloc.return_value.init.return_value
    # NotDetermined throughout — even after a denied callback the status
    # sequence here just keeps returning 0; the helper short-circuits
    # before the second status read.
    store.authorizationStatusForEntityType_.return_value = 0

    def fake_request(entity_type, completion):
        completion(False, None)  # User clicks "Deny".
    store.requestAccessToEntityType_completion_.side_effect = fake_request

    with patch(
        "opencomputer.profile_bootstrap.calendar_reader._import_event_kit",
        return_value=fake_ek,
    ):
        events = read_upcoming_events(days=7)

    store.requestAccessToEntityType_completion_.assert_called_once()
    assert events == []


def test_request_calendar_access_returns_false_on_timeout():
    """If the user doesn't respond within timeout, helper returns False."""
    fake_ek = MagicMock()
    fake_store = MagicMock()

    def never_completes(entity_type, completion):
        pass  # Never invoke completion — simulates user ignoring dialog.
    fake_store.requestAccessToEntityType_completion_.side_effect = never_completes

    granted = _request_calendar_access(
        fake_store, fake_ek, timeout_seconds=0.05,
    )
    assert granted is False


def test_request_calendar_access_returns_true_when_granted():
    """Synchronous callback with granted=True → helper returns True."""
    fake_ek = MagicMock()
    fake_store = MagicMock()

    def grant(entity_type, completion):
        completion(True, None)
    fake_store.requestAccessToEntityType_completion_.side_effect = grant

    granted = _request_calendar_access(
        fake_store, fake_ek, timeout_seconds=1.0,
    )
    assert granted is True


def test_request_calendar_access_logs_error_when_present():
    """Error in callback is logged but does not crash; granted value still returned."""
    fake_ek = MagicMock()
    fake_store = MagicMock()

    def deny_with_error(entity_type, completion):
        completion(False, "fake-NSError")
    fake_store.requestAccessToEntityType_completion_.side_effect = deny_with_error

    granted = _request_calendar_access(
        fake_store, fake_ek, timeout_seconds=1.0,
    )
    assert granted is False
