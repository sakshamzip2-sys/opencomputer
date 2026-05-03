"""
Typed signal events — the public pub/sub vocabulary (Phase 3.A, F2).

Plugins and internal subscribers read these types to participate in the
:class:`opencomputer.ingestion.bus.TypedEventBus` fanout. The bus itself
lives in the core package (``opencomputer/ingestion/bus.py``); it imports
the types declared here so plugins never have to reach into
``opencomputer/*`` to consume or emit signals.

Stability contract
------------------

This module is part of the public SDK. Once a subclass of
:class:`SignalEvent` is shipped, its field set is frozen for backwards
compatibility — adding NEW subclasses or NEW optional fields to an
existing subclass is safe, but renaming / removing / re-typing a field
is a **breaking change**. Breaking changes must be announced in
``docs/parallel-sessions.md`` under "Bus API change log" so parallel
sessions (Session B's B3 trajectory subscriber in particular) can
coordinate migrations.

Privacy posture
---------------

Concrete event types are shaped to **avoid carrying raw user content**
across subscribers. :class:`MessageEvent` exposes ``content_length``,
not the message body. :class:`WebObservationEvent` carries
``payload_size_bytes`` rather than the scraped document. Subscribers
that need richer context retrieve it from their own persisted state
(SessionDB for messages, the scraper's own store for web pages) keyed
by the ids attached to the event. This keeps the bus "metadata only"
and compatible with Session B's evolution privacy rule (design §4.1:
no string value > 200 chars in trajectory metadata).

Typical consumer
----------------

Subscribers import the bus singleton from
``opencomputer.ingestion.bus`` (``default_bus``) and call
``default_bus.subscribe("tool_call", handler)``. The handler receives
a :class:`ToolCallEvent`; it accesses ``event.tool_name``,
``event.outcome``, ``event.duration_seconds`` as needed. The returned
:class:`~opencomputer.ingestion.bus.Subscription` exposes an
``unsubscribe()`` method for clean removal.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Discriminator literal types (exported for subscriber type annotations)
# ---------------------------------------------------------------------------

ToolCallOutcome = Literal["success", "failure", "blocked", "cancelled"]
WebContentKind = Literal["html", "json", "text", "markdown"]
FileOperation = Literal["read", "write", "stat", "delete", "list"]
MessageRole = Literal["user", "assistant", "system", "tool"]
HookDecisionKind = Literal["pass", "approve", "block"]


# ---------------------------------------------------------------------------
# Base event
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """Base class for every typed event on the bus.

    Attributes
    ----------
    event_id:
        Stable UUID4 per event, auto-generated on construction. Used by
        subscribers for idempotency / dedup.
    event_type:
        Discriminator string. Subclasses override this via their
        default — e.g. ``"tool_call"``, ``"web_observation"``. The bus
        uses this value for type-keyed fanout; glob patterns (e.g.
        ``"web_*"``) match against it.
    timestamp:
        Unix epoch seconds (float). Default = ``time.time()`` at
        construction; callers may override for back-dated events.
    session_id:
        The agent session that emitted this event, or ``None`` for
        system-emitted events (e.g. startup hook).
    source:
        Short identifier for the emitter. Convention: ``"agent_loop"``,
        ``"web_fetch"``, ``"introspection"``. Useful for per-publisher
        metrics.
    metadata:
        Arbitrary JSON-serializable structured data. Subclasses add
        typed fields for the common case; ``metadata`` is the escape
        hatch. Keep entries small — this is the bus, not the database.
    """

    event_type: str = ""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    session_id: str | None = None
    source: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Concrete subclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCallEvent(SignalEvent):
    """A tool invocation reached a terminal state.

    Emitted by the agent loop after each tool call completes (success,
    failure, or blocked). One event per call, even for parallel
    batches — subscribers correlate across calls via ``session_id`` +
    ``timestamp`` if they need batch-level context.
    """

    event_type: str = "tool_call"
    tool_name: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    outcome: ToolCallOutcome = "success"
    duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class WebObservationEvent(SignalEvent):
    """A web fetch / scrape produced a payload.

    Web fetchers (e.g. WebFetch tool, OI bridge) publish these.
    The payload itself stays in the fetcher's store; this event
    carries addressing (url + domain) + shape metadata.
    """

    event_type: str = "web_observation"
    url: str = ""
    domain: str = ""
    content_kind: WebContentKind = "html"
    payload_size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class FileObservationEvent(SignalEvent):
    """A local-filesystem operation reached a terminal state.

    Read / write / stat / delete / list. ``size_bytes`` is ``None``
    when the operation doesn't have a canonical size (stat on a dir,
    list, etc.) — subscribers must tolerate that.
    """

    event_type: str = "file_observation"
    path: str = ""
    operation: FileOperation = "read"
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class MessageSignalEvent(SignalEvent):
    """An assistant / user / system / tool message crossed a boundary.

    Named ``MessageSignalEvent`` (not ``MessageEvent``) to avoid
    shadowing :class:`plugin_sdk.core.MessageEvent`, the unrelated
    platform-agnostic inbound-message dataclass used by channel
    adapters. The discriminator ``event_type`` is simply ``"message"``
    — subscribers match on that, not the class name.

    Note the **privacy posture**: this event does NOT carry the message
    content. Subscribers that need the body retrieve it from SessionDB
    by ``session_id`` + ``event_id``.
    """

    event_type: str = "message"
    role: MessageRole = "user"
    content_length: int = 0


@dataclass(frozen=True, slots=True)
class HookSignalEvent(SignalEvent):
    """A lifecycle hook resolved to a decision.

    Named ``HookSignalEvent`` (not ``HookEvent``) to avoid colliding with
    the existing :class:`plugin_sdk.hooks.HookEvent` enum that names the
    nine lifecycle events. The discriminator ``event_type`` is simply
    ``"hook"`` — subscribers use that, not the class name.
    """

    event_type: str = "hook"
    hook_name: str = ""
    decision: HookDecisionKind = "pass"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TurnStartEvent(SignalEvent):
    """Fires at the top of each agent turn iteration.

    Allows MemoryProvider hooks (via MemoryBridge.register_with_bus)
    to trigger fresh prefetch, telemetry, or side-channel logic at
    the start of each turn without requiring plugin_sdk extensions.

    PR-8 of Hermes parity plan.
    """

    event_type: str = field(default="turn_start", init=False)
    turn_index: int = 0


@dataclass(frozen=True, slots=True)
class PolicyChangeEvent(SignalEvent):
    """Fires when a policy decision changes status.

    Phase 2 v0 of outcome-aware learning. Emitted by the engine cron
    on draft + apply, by slash commands on approve/revert, and by the
    auto-revert sweep. Subscribers (Telegram extension, dashboards,
    custom reactors) consume the event to surface or react to policy
    decisions without coupling to the cron path.
    """

    event_type: str = field(default="policy_change", init=False)
    change_id: str = ""
    knob_kind: str = ""
    target_id: str = ""
    status: str = ""
    approval_mode: str = ""
    engine_version: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PolicyRevertedEvent(SignalEvent):
    """Fires when a policy_change reaches status='reverted'.

    Distinguished from PolicyChangeEvent so subscribers can react
    specifically to rollbacks (e.g., user-visible notification 'change X
    was rolled back because metric Y degraded')."""

    event_type: str = field(default="policy_reverted", init=False)
    change_id: str = ""
    knob_kind: str = ""
    target_id: str = ""
    reverted_reason: str = ""


@dataclass(frozen=True, slots=True)
class TurnCompletedEvent(SignalEvent):
    """Fires after a turn's ``turn_outcomes`` row has been written.

    Phase 0 of outcome-aware learning. Publishes the same payload that
    just landed in the DB so any subscriber (Honcho extension, future
    analytics dashboards, custom reactors) can observe it without
    coupling to the dispatch path. Decoupling the consumer side avoids
    SDK-boundary violations from dispatch.py importing extensions.

    The ``signals`` field is a dict mirror of ``TurnSignals`` —
    JSON-serialisable for easy persistence by subscribers.
    """

    event_type: str = field(default="turn_completed", init=False)
    turn_index: int = 0
    signals: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DelegationCompleteEvent(SignalEvent):
    """Fires after DelegateTool subagent finishes.

    Allows memory providers and bus subscribers to observe the end of
    each subagent delegation — e.g. to flush per-session state or
    trigger cross-session summarisation. The ``child_outcome`` field
    is one of ``"success"``, ``"failure"``, or ``"error"``.

    PR-8 of Hermes parity plan.
    """

    event_type: str = field(default="delegation_complete", init=False)
    parent_session_id: str = ""
    child_session_id: str = ""
    child_outcome: str = "success"  # "success" | "failure" | "error"


@dataclass(frozen=True, slots=True)
class MemoryWriteEvent(SignalEvent):
    """Fires when a declarative-memory write happens.

    Privacy posture: carries ``content_size`` only — NOT the content
    being written — so subscribers cannot reconstruct the memory body
    from bus traffic alone. Useful for audit patterns, quota
    enforcement, and provider cache-invalidation.

    PR-8 of Hermes parity plan.
    """

    event_type: str = field(default="memory_write", init=False)
    action: str = ""    # "append" | "replace" | "remove" | etc.
    target: str = ""    # which file (e.g. "MEMORY.md" / "USER.md")
    content_size: int = 0


@dataclass(frozen=True, slots=True)
class ForegroundAppEvent(SignalEvent):
    """Foreground app or window-title change observed by ambient-sensors plugin.

    Privacy contract:

    - ``window_title_hash`` is SHA-256 of the title — raw title NEVER leaves
      the sensor. Hashes are useful only as a per-process dedup token.
    - When the sensor's sensitive-app filter matches, the publisher replaces
      ``app_name`` with ``"<filtered>"``, ``window_title_hash`` with the
      empty string, and sets ``is_sensitive=True``. Subscribers thus never
      see filtered raw values.
    - All fields default to empty/false so an under-filled instance can't
      accidentally publish meaningful data.
    """

    event_type: str = "foreground_app"
    app_name: str = ""
    window_title_hash: str = ""
    bundle_id: str = ""
    is_sensitive: bool = False
    platform: str = ""


@dataclass(frozen=True, slots=True)
class AmbientSensorPauseEvent(SignalEvent):
    """An ambient sensor entered or exited a paused/disabled state.

    Subscribers (e.g. the motif extractor) use these events so gaps in
    the foreground stream aren't misattributed to user idleness.
    """

    event_type: str = "ambient_sensor_pause"
    sensor_name: str = "foreground"
    paused: bool = True
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SessionEndEvent(SignalEvent):
    """Session reached a terminal state.

    Emitted by the agent loop when a session ends — naturally via
    END_TURN, via error, via user cancellation, or via timeout. Lets
    subscribers (analytics, evolution, finalization) react without
    polling the SessionDB.
    """

    event_type: str = "session_end"
    end_reason: str = "completed"      # "completed" | "error" | "cancelled" | "timeout"
    turn_count: int = 0
    duration_seconds: float = 0.0
    had_errors: bool = False


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


class SignalNormalizer(ABC):
    """Convert a raw object into a :class:`SignalEvent` (or skip it).

    Normalizers let publishers adapt third-party objects (e.g. an
    httpx ``Response``, a subprocess ``CompletedProcess``) into the
    bus's typed vocabulary without pushing that logic into every
    call site. A normalizer returning ``None`` means "I recognise
    this input but don't want to emit an event" — the publisher
    skips the publish call.
    """

    @abstractmethod
    def normalize(self, raw: Any) -> SignalEvent | None:
        """Return a ``SignalEvent`` for ``raw``, or ``None`` to skip."""
        raise NotImplementedError


class IdentityNormalizer(SignalNormalizer):
    """Pass-through for inputs that are already :class:`SignalEvent`s.

    Useful as the terminal normalizer in a chain, and as a safe
    default when the publisher already constructs typed events.
    """

    def normalize(self, raw: Any) -> SignalEvent | None:
        if isinstance(raw, SignalEvent):
            return raw
        return None


# ---------------------------------------------------------------------------
# Module-level normalizer registry
# ---------------------------------------------------------------------------


_NORMALIZERS: dict[str, SignalNormalizer] = {}


def register_normalizer(event_type: str, normalizer: SignalNormalizer) -> None:
    """Register ``normalizer`` as the default for ``event_type``.

    Re-registering an existing ``event_type`` overwrites the prior
    entry (last-registered wins). No locking — callers are expected
    to register at import / startup time, not from concurrent
    request paths.
    """
    if not isinstance(normalizer, SignalNormalizer):
        raise TypeError(
            "normalizer must be a SignalNormalizer instance"
        )
    _NORMALIZERS[event_type] = normalizer


def get_normalizer(event_type: str) -> SignalNormalizer | None:
    """Return the normalizer registered for ``event_type``, or ``None``."""
    return _NORMALIZERS.get(event_type)


def clear_normalizers() -> None:
    """Remove every registered normalizer. Test-only helper."""
    _NORMALIZERS.clear()


__all__ = [
    # literal aliases
    "ToolCallOutcome",
    "WebContentKind",
    "FileOperation",
    "MessageRole",
    "HookDecisionKind",
    # base
    "SignalEvent",
    # concrete subclasses
    "ToolCallEvent",
    "WebObservationEvent",
    "FileObservationEvent",
    "MessageSignalEvent",
    "HookSignalEvent",
    # PR-8: bus-driven memory hooks (T3.2)
    "TurnStartEvent",
    "TurnCompletedEvent",
    "PolicyChangeEvent",
    "PolicyRevertedEvent",
    "DelegationCompleteEvent",
    "MemoryWriteEvent",
    # T1 of ambient foreground sensor plan (2026-04-27)
    "ForegroundAppEvent",
    "AmbientSensorPauseEvent",
    # T1 of auto-skill-evolution plan (2026-04-27)
    "SessionEndEvent",
    # normalizer
    "SignalNormalizer",
    "IdentityNormalizer",
    "register_normalizer",
    "get_normalizer",
    "clear_normalizers",
]
