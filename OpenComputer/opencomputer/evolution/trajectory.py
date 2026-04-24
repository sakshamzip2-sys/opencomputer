"""Trajectory dataclasses for OpenComputer Evolution.

This is the leaf data module — imports stdlib only.  No opencomputer/* imports
here so the dataclasses can be used from any layer without circular dependencies.

Design reference: OpenComputer/docs/evolution/design.md §4.1 and §4.2.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Module-level constant
# ---------------------------------------------------------------------------

SCHEMA_VERSION_CURRENT: int = 1
"""Schema version for records built by this module version.

Bump when TrajectoryRecord or TrajectoryEvent field layouts change in a
backward-incompatible way.  Old records remain readable — callers check
``record.schema_version`` to detect older layouts.
"""


# ---------------------------------------------------------------------------
# TrajectoryEvent
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class TrajectoryEvent:
    """Single (state, action, outcome) tuple captured during an agent session.

    **Privacy rule** (design doc §4.1): ``metadata`` stores tool-name-level
    signals only — counts, exit codes, sizes, ids.  Any string value longer
    than 200 characters is rejected at construction time with a ``ValueError``
    so that raw prompt text can never leak into the evolution store.  Non-string
    metadata values (int, float, list, dict, None, …) are not subject to the
    length limit.  All metadata keys must be strings.
    """

    session_id: str
    """FK into agent_state.sessions.id — NOT inlined session content."""

    message_id: int | None
    """FK into agent_state.messages.id, when applicable."""

    action_type: str
    """One of: "tool_call" | "user_reply" | "assistant_reply" | "error"."""

    tool_name: str | None
    """PascalCase tool name when action_type == "tool_call", else None."""

    outcome: str
    """One of: "success" | "failure" | "blocked_by_hook" | "user_cancelled"."""

    timestamp: float
    """Unix epoch seconds."""

    metadata: Mapping[str, Any]
    """Tool-specific extras.  No raw prompt text — see privacy rule above."""

    def __post_init__(self) -> None:
        # Validate metadata keys and string-value lengths.
        for key, value in self.metadata.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"metadata key {key!r} is not a string — "
                    "evolution metadata keys must be strings"
                )
            if isinstance(value, str) and len(value) > 200:
                raise ValueError(
                    f"metadata field {key!r} looks like raw prompt text — "
                    "evolution stores tool-name + outcome only"
                )


# ---------------------------------------------------------------------------
# TrajectoryRecord
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class TrajectoryRecord:
    """Session-bounded, ordered sequence of TrajectoryEvents.

    ``events`` is a ``tuple``, not a list, to preserve immutability with the
    frozen dataclass.  Callers building records incrementally should use
    ``with_event()`` which returns a new record with the event appended.
    """

    id: int | None
    """Primary key assigned at storage insert.  ``None`` pre-insert."""

    session_id: str
    """Matches the session_id on every contained TrajectoryEvent."""

    schema_version: int
    """Schema version for this record.  Use ``SCHEMA_VERSION_CURRENT`` when building."""

    started_at: float
    """Unix epoch seconds when the session started."""

    ended_at: float | None
    """Unix epoch seconds when the session ended.  ``None`` while ongoing."""

    events: tuple[TrajectoryEvent, ...]
    """Ordered event sequence.  Must be a tuple — list is not accepted."""

    completion_flag: bool
    """True when the session reached a clean terminal state."""

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple):
            raise TypeError(
                f"TrajectoryRecord.events must be a tuple, got {type(self.events).__name__}"
            )


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def new_event(
    *,
    session_id: str,
    action_type: str,
    outcome: str,
    message_id: int | None = None,
    tool_name: str | None = None,
    timestamp: float | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> TrajectoryEvent:
    """Convenience constructor for TrajectoryEvent.

    Sets ``timestamp`` to ``time.time()`` when not provided.
    """
    return TrajectoryEvent(
        session_id=session_id,
        message_id=message_id,
        action_type=action_type,
        tool_name=tool_name,
        outcome=outcome,
        timestamp=timestamp if timestamp is not None else time.time(),
        metadata=metadata if metadata is not None else {},
    )


def new_record(
    session_id: str,
    *,
    started_at: float | None = None,
) -> TrajectoryRecord:
    """Return an empty TrajectoryRecord with sensible defaults.

    ``id`` is ``None`` (pre-insert), ``schema_version`` is
    ``SCHEMA_VERSION_CURRENT``, ``events`` is an empty tuple,
    ``ended_at`` is ``None``, ``completion_flag`` is ``False``.
    """
    return TrajectoryRecord(
        id=None,
        session_id=session_id,
        schema_version=SCHEMA_VERSION_CURRENT,
        started_at=started_at if started_at is not None else time.time(),
        ended_at=None,
        events=(),
        completion_flag=False,
    )


def with_event(record: TrajectoryRecord, event: TrajectoryEvent) -> TrajectoryRecord:
    """Return a new TrajectoryRecord with *event* appended to ``events``.

    The original record is not modified (frozen dataclass).  Uses
    ``dataclasses.replace`` to copy all fields except ``events``.
    """
    return dataclasses.replace(record, events=(*record.events, event))
