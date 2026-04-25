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


# ---------------------------------------------------------------------------
# Bus subscriber (B3) — auto-collect trajectories from the F2 TypedEvent bus
# ---------------------------------------------------------------------------
# ruff: noqa: E402 — stdlib-only imports appended to leaf module per design
import logging  # noqa: E402
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from opencomputer.ingestion.bus import Subscription, TypedEventBus  # noqa: F401
    from plugin_sdk.ingestion import ToolCallEvent  # noqa: F401

logger = logging.getLogger(__name__)

# In-memory "open trajectory per session_id" — events accumulate until session ends
_open_trajectories: dict[str, TrajectoryRecord] = {}


def _on_tool_call_event(event: ToolCallEvent) -> None:
    """Bus handler — converts a ToolCallEvent into a TrajectoryEvent and either
    appends to an open trajectory (same session_id) or starts a new one.

    NEVER raises — exceptions are logged + swallowed so the bus's
    exception-isolated fanout protects unrelated subscribers.
    """
    try:
        if event.session_id is None:
            return  # cannot bucket anonymously

        # Build the TrajectoryEvent — privacy rule already enforced by TrajectoryEvent.__post_init__
        metadata: dict[str, Any] = {
            "duration_seconds": float(event.duration_seconds),
            # Preserve metadata values that pass the 200-char privacy filter.
            # T3.1 (PR-8): error_class and error_message_preview are first-class
            # fields the reflection prompt knows how to render. They are already
            # truncated to 200 chars by the loop publisher, so the filter below
            # admits them without further truncation.
            **{
                k: v
                for k, v in (event.metadata or {}).items()
                if not isinstance(v, str) or len(v) <= 200
            },
        }
        # Explicit setdefault calls below are documentation that these two keys
        # are intentionally forwarded to the trajectory (they are already covered
        # by the dict comprehension above; these are no-ops in the normal path).
        if event.metadata and event.metadata.get("error_class"):
            metadata.setdefault("error_class", event.metadata["error_class"])
        if event.metadata and event.metadata.get("error_message_preview"):
            metadata.setdefault("error_message_preview", event.metadata["error_message_preview"])

        traj_event = TrajectoryEvent(
            session_id=event.session_id,
            message_id=None,
            action_type="tool_call",
            tool_name=event.tool_name,
            outcome=event.outcome,
            timestamp=event.timestamp,
            metadata=metadata,
        )

        # Append to open trajectory or start fresh
        existing = _open_trajectories.get(event.session_id)
        if existing is None:
            existing = new_record(event.session_id, started_at=event.timestamp)
        existing = with_event(existing, traj_event)
        _open_trajectories[event.session_id] = existing

    except Exception:
        logger.exception("evolution: trajectory subscriber failed for event %r", event)


def _on_session_end(session_id: str) -> int | None:
    """Mark an open trajectory as ended + persist it. Returns the inserted id, or None
    if no trajectory was open for that session.
    """
    try:
        import time as _time
        from dataclasses import replace as dc_replace

        from opencomputer.evolution.reward import RuleBasedRewardFunction
        from opencomputer.evolution.storage import insert_record, update_reward

        record = _open_trajectories.pop(session_id, None)
        if record is None:
            return None

        ended = dc_replace(record, ended_at=_time.time(), completion_flag=True)
        record_id = insert_record(ended)

        # Compute reward (RuleBasedRewardFunction default) + persist it
        reward = RuleBasedRewardFunction().score(ended)
        if reward is not None:
            update_reward(record_id, reward)

        return record_id
    except Exception:
        logger.exception("evolution: session-end persistence failed for session %r", session_id)
        return None


def register_with_bus(bus: TypedEventBus | None = None) -> Subscription:
    """Subscribe `_on_tool_call_event` to the TypedEvent bus.

    If `bus` is None, uses `get_default_bus()`. Returns the Subscription for later
    unregistration. Idempotent — safe to call multiple times (multiple subscriptions
    will fire — caller is responsible for deduping via the returned handle).
    """
    if bus is None:
        from opencomputer.ingestion.bus import get_default_bus

        bus = get_default_bus()
    return bus.subscribe("tool_call", _on_tool_call_event)


def is_collection_enabled() -> bool:
    """Check the on-disk flag at <evolution_home() / 'enabled'>."""
    from opencomputer.evolution.storage import evolution_home

    return (evolution_home() / "enabled").exists()


def set_collection_enabled(enabled: bool) -> None:
    """Toggle the on-disk flag."""
    from opencomputer.evolution.storage import evolution_home

    flag = evolution_home() / "enabled"
    if enabled:
        flag.touch()
    elif flag.exists():
        flag.unlink()


def bootstrap_if_enabled() -> Subscription | None:
    """Auto-register the bus subscriber if the on-disk flag is set.

    Callers (e.g. AgentLoop initialization, opencomputer CLI startup) can invoke
    this to opt into auto-collection. Returns the Subscription, or None if not enabled.
    """
    if not is_collection_enabled():
        return None
    return register_with_bus()
