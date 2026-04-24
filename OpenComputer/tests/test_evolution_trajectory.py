"""Tests for opencomputer.evolution.trajectory — dataclasses, helpers, privacy rules.

All tests are pure unit tests; no I/O, no mocks needed.
"""

from __future__ import annotations

import dataclasses
import time

import pytest

from opencomputer.evolution.trajectory import (
    SCHEMA_VERSION_CURRENT,
    TrajectoryEvent,
    TrajectoryRecord,
    new_event,
    new_record,
    with_event,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_event(**kwargs) -> TrajectoryEvent:
    """Build a minimal valid TrajectoryEvent, overridable via kwargs."""
    defaults = dict(
        session_id="sess-abc",
        message_id=None,
        action_type="tool_call",
        tool_name="Read",
        outcome="success",
        timestamp=1_700_000_000.0,
        metadata={},
    )
    defaults.update(kwargs)
    return TrajectoryEvent(**defaults)


def _minimal_record(**kwargs) -> TrajectoryRecord:
    """Build a minimal valid TrajectoryRecord, overridable via kwargs."""
    defaults = dict(
        id=None,
        session_id="sess-abc",
        schema_version=SCHEMA_VERSION_CURRENT,
        started_at=1_700_000_000.0,
        ended_at=None,
        events=(),
        completion_flag=False,
    )
    defaults.update(kwargs)
    return TrajectoryRecord(**defaults)


# ---------------------------------------------------------------------------
# 1. TrajectoryEvent is frozen
# ---------------------------------------------------------------------------


def test_trajectory_event_is_frozen() -> None:
    """Mutating any field on TrajectoryEvent raises FrozenInstanceError."""
    event = _minimal_event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.outcome = "failure"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. TrajectoryEvent uses slots
# ---------------------------------------------------------------------------


def test_trajectory_event_uses_slots() -> None:
    """TrajectoryEvent has __slots__ and rejects arbitrary attribute assignment.

    Python 3.13 frozen+slots dataclasses raise TypeError (instead of AttributeError)
    when assigning to a non-slot attribute because the frozen __setattr__ calls
    super() in a context where the slots MRO lookup fails.  We accept both.
    """
    assert hasattr(TrajectoryEvent, "__slots__"), "Expected __slots__ on TrajectoryEvent"
    event = _minimal_event()
    with pytest.raises((AttributeError, TypeError)):
        event.nonexistent_field = "oops"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 3. TrajectoryEvent equality is structural
# ---------------------------------------------------------------------------


def test_trajectory_event_structural_equality() -> None:
    """Two TrajectoryEvent instances with identical fields compare equal."""
    ev1 = _minimal_event()
    ev2 = _minimal_event()
    assert ev1 == ev2


# ---------------------------------------------------------------------------
# 4. Privacy rule — long string value rejected
# ---------------------------------------------------------------------------


def test_trajectory_event_privacy_rule_long_string_rejected() -> None:
    """metadata string value > 200 chars raises ValueError mentioning 'metadata'."""
    with pytest.raises(ValueError, match="metadata"):
        _minimal_event(metadata={"foo": "x" * 201})


# ---------------------------------------------------------------------------
# 5. Privacy rule — short string value allowed
# ---------------------------------------------------------------------------


def test_trajectory_event_privacy_rule_short_string_allowed() -> None:
    """metadata string value <= 200 chars is accepted without error."""
    event = _minimal_event(metadata={"foo": "short"})
    assert event.metadata["foo"] == "short"


# ---------------------------------------------------------------------------
# 6. Privacy rule — non-string values are NOT length-checked
# ---------------------------------------------------------------------------


def test_trajectory_event_privacy_rule_nonstring_values_not_length_checked() -> None:
    """int, list, dict, and None values in metadata are not subject to the length limit."""
    # These must all succeed without ValueError.
    _minimal_event(metadata={"count": 42})
    _minimal_event(metadata={"items": [1, 2, 3]})
    _minimal_event(metadata={"nested": {"a": "b"}})
    _minimal_event(metadata={"nothing": None})
    # A list containing a long string — the list itself is not a str, so no error.
    _minimal_event(metadata={"raw_list": ["x" * 300]})


# ---------------------------------------------------------------------------
# 7. Non-string metadata keys rejected
# ---------------------------------------------------------------------------


def test_trajectory_event_nonstring_keys_rejected() -> None:
    """metadata with a non-string key raises ValueError."""
    with pytest.raises(ValueError):
        _minimal_event(metadata={123: "ok"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 8. TrajectoryRecord is frozen and uses slots
# ---------------------------------------------------------------------------


def test_trajectory_record_is_frozen() -> None:
    """Mutating any field on TrajectoryRecord raises FrozenInstanceError."""
    record = _minimal_record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.completion_flag = True  # type: ignore[misc]


def test_trajectory_record_uses_slots() -> None:
    """TrajectoryRecord has __slots__ and rejects arbitrary attribute assignment.

    Python 3.13 frozen+slots dataclasses raise TypeError (instead of AttributeError)
    when assigning to a non-slot attribute because the frozen __setattr__ calls
    super() in a context where the slots MRO lookup fails.  We accept both.
    """
    assert hasattr(TrajectoryRecord, "__slots__"), "Expected __slots__ on TrajectoryRecord"
    record = _minimal_record()
    with pytest.raises((AttributeError, TypeError)):
        record.nonexistent_field = "oops"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 9. events must be a tuple — list raises TypeError
# ---------------------------------------------------------------------------


def test_trajectory_record_events_must_be_tuple() -> None:
    """Passing a list for events raises TypeError (not a tuple)."""
    with pytest.raises(TypeError):
        _minimal_record(events=[])  # type: ignore[arg-type]


def test_trajectory_record_events_accepts_tuple() -> None:
    """Passing a tuple (including empty) for events is accepted."""
    record = _minimal_record(events=())
    assert record.events == ()

    ev = _minimal_event()
    record2 = _minimal_record(events=(ev,))
    assert len(record2.events) == 1


# ---------------------------------------------------------------------------
# 10. SCHEMA_VERSION_CURRENT == 1
# ---------------------------------------------------------------------------


def test_schema_version_current_is_one() -> None:
    """SCHEMA_VERSION_CURRENT must equal 1 for B1."""
    assert SCHEMA_VERSION_CURRENT == 1


# ---------------------------------------------------------------------------
# 11. new_event defaults timestamp to ~now
# ---------------------------------------------------------------------------


def test_new_event_defaults_timestamp_to_now() -> None:
    """new_event() sets timestamp close to time.time() when not supplied."""
    before = time.time()
    ev = new_event(session_id="s", action_type="tool_call", outcome="success")
    after = time.time()
    assert before <= ev.timestamp <= after + 1.0


# ---------------------------------------------------------------------------
# 12. new_record returns expected defaults
# ---------------------------------------------------------------------------


def test_new_record_defaults() -> None:
    """new_record() returns a record with id=None, events=(), schema_version==SCHEMA_VERSION_CURRENT."""
    rec = new_record("sess-xyz")
    assert rec.id is None
    assert rec.events == ()
    assert rec.schema_version == SCHEMA_VERSION_CURRENT
    assert rec.ended_at is None
    assert rec.completion_flag is False
    assert rec.session_id == "sess-xyz"


# ---------------------------------------------------------------------------
# 13. with_event appends and preserves immutability
# ---------------------------------------------------------------------------


def test_with_event_appends_and_preserves_original() -> None:
    """with_event returns a new record with event appended; original is unchanged."""
    original = new_record("sess-q")
    ev1 = _minimal_event(session_id="sess-q")
    ev2 = _minimal_event(session_id="sess-q", action_type="user_reply", tool_name=None)

    rec_with_one = with_event(original, ev1)
    rec_with_two = with_event(rec_with_one, ev2)

    # Original untouched.
    assert len(original.events) == 0

    # Intermediate untouched.
    assert len(rec_with_one.events) == 1

    # Final has both.
    assert len(rec_with_two.events) == 2
    assert rec_with_two.events[0] == ev1
    assert rec_with_two.events[1] == ev2
