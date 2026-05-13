"""Per-method and per-event typed schemas for the wire protocol.

`protocol.py` (v1) ships `WireRequest.params: dict[str, Any]` — generic,
works, but loses every type guarantee at the wire boundary. Wire clients
(TUI, web dashboard, IDE bridges) silently desync when a new field lands
on either side.

Phase 12g adds typed param/payload schemas per method and per event,
WITHOUT breaking v1. The base WireRequest/Response/Event types from v1
are re-exported here so callers can `from opencomputer.gateway.protocol_v2
import WireRequest` and progressively migrate.

Migration shape:
    # v1 (still works)
    req = WireRequest(id="1", method="chat", params={"message": "hi"})

    # v2 (typed)
    params = ChatParams(message="hi", session_id=None)
    req = WireRequest(id="1", method=METHOD_CHAT, params=params.model_dump())

The wire encoding is identical — both serialise to the same JSON. The
benefit is compile-time + runtime validation on either side.

Source: openclaw `src/gateway/protocol/schema/*.ts` per-domain schema
pattern. Phase 12g.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Re-export v1 base shapes so v2 callers don't need both imports.
from opencomputer.gateway.protocol import (
    EVENT_ASSISTANT_MESSAGE,
    EVENT_ERROR,
    EVENT_EVOLUTION_TUNING_CHANGED,
    EVENT_MEMORY_WRITE,
    EVENT_PERMISSION_REQUEST,
    EVENT_PROFILE_SWAP,
    EVENT_STREAM_RETRY,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    EVENT_TURN_BEGIN,
    EVENT_TURN_END,
    METHOD_CHAT,
    METHOD_EVOLUTION_STATUS,
    METHOD_HELLO,
    METHOD_MEMORY_STATUS,
    METHOD_PERMISSION_RESPONSE,
    METHOD_SEARCH,
    METHOD_SESSION_LIST,
    METHOD_SKILLS_LIST,
    METHOD_SLASH_DISPATCH,
    METHOD_SLASH_LIST,
    METHOD_STEER_SUBMIT,
    WireEvent,
    WireRequest,
    WireResponse,
)


class _StrictModel(BaseModel):
    """Base for all v2 schemas — extra=forbid + frozen so wire payloads
    can't accumulate stale fields silently."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ─── Per-method param schemas ──────────────────────────────────────────


class HelloParams(_StrictModel):
    """Client capability declaration sent on connect.

    v1.1 plan-1 M3.3 (2026-05-09) — :attr:`session_id` +
    :attr:`last_event_seq` enable wire-reconnect replay. After the
    server returns :class:`HelloResult`, it replays any events from
    the per-session ring buffer with ``seq > last_event_seq`` so a
    transient network drop doesn't cost the client visibility into
    intermediate tool calls. Both fields are optional so old clients
    that never disconnect mid-turn keep working unchanged.
    """

    client: str  # e.g. "opencomputer-tui/0.1.0"
    capabilities: tuple[str, ...] = ()
    session_id: str | None = None
    last_event_seq: int | None = None


class HelloResult(_StrictModel):
    """Gateway capability response.

    v1.1 plan-1 M3.3 (2026-05-09) — when the client passes
    :attr:`HelloParams.last_event_seq`, the server replays missed
    events from the per-session ring buffer immediately after this
    HelloResult and sets :attr:`gap_warning` if any events were lost
    (i.e. the last_event_seq is older than the buffer can represent).
    """

    server: str  # e.g. "opencomputer/0.1.0"
    capabilities: tuple[str, ...]
    protocol_version: int = 2
    #: True when the client requested replay via ``last_event_seq`` and
    #: that seq fell off the end of the ring buffer, so some events
    #: were lost. Old clients (no ``last_event_seq``) always see False.
    gap_warning: bool = False
    #: Echoes the highest seq currently held in the per-session ring,
    #: so reconnecting clients can detect immediately whether they
    #: have a gap to worry about.
    server_last_event_seq: int | None = None


class ChatParams(_StrictModel):
    """Send a user message; receive an assistant response (synchronous)."""

    message: str
    session_id: str | None = None
    plan_mode: bool = False
    #: Canonical permission mode (default | plan | accept-edits | auto). Old
    #: clients that omit this field still decode fine; servers fall back to
    #: the legacy ``plan_mode`` bool when ``permission_mode == "default"``.
    permission_mode: str = "default"


class ChatResult(_StrictModel):
    final_message: str
    session_id: str
    iterations: int
    input_tokens: int
    output_tokens: int


class SessionListParams(_StrictModel):
    limit: int = 20


class SessionListResult(_StrictModel):
    sessions: tuple[dict[str, Any], ...]


class SearchParams(_StrictModel):
    query: str
    limit: int = 20


class SearchResult(_StrictModel):
    hits: tuple[dict[str, Any], ...]


class SkillsListParams(_StrictModel):
    pass


class SkillsListResult(_StrictModel):
    skills: tuple[dict[str, Any], ...]


class SteerSubmitParams(_StrictModel):
    """Round 2a P-2 — submit a mid-run nudge to a session.

    Latest-wins: a fresh submit replaces any pending nudge for the
    same ``session_id``. The agent loop consumes the nudge between
    turns and prepends it to the next LLM request as a synthetic
    user message.
    """

    session_id: str
    prompt: str


class SteerSubmitResult(_StrictModel):
    session_id: str
    #: True if a previous (now-discarded) nudge was already pending.
    had_pending: bool
    queued_chars: int


# 2026-05-07 PR6 — slash commands routed via wire (single source of truth
# for the dashboard ChatPage palette and the Ink TUI).


class SlashListParams(_StrictModel):
    pass


class SlashCommandInfo(_StrictModel):
    name: str
    description: str
    aliases: tuple[str, ...] = ()


class SlashListResult(_StrictModel):
    commands: tuple[SlashCommandInfo, ...]


class SlashDispatchParams(_StrictModel):
    name: str
    args: str = ""
    session_id: str | None = None


class SlashDispatchResult(_StrictModel):
    output: str
    side_effects: dict[str, Any] = Field(default_factory=dict)


# v1.1 plan-1 M3.1 (2026-05-09) — permission request/response wire surface
# for Tier-2 consent gates with no channel adapter bound.


class PermissionRequestPayload(_StrictModel):
    """Server → client event when a Tier-2 capability needs approval.

    Emitted by ``EVENT_PERMISSION_REQUEST`` on a wire-bound session
    that hits a Tier-2 capability without a channel adapter present
    to ask the user. The client (TUI / IDE / dashboard) renders an
    approval prompt to the user and replies via
    :class:`PermissionResponseParams` keyed on ``request_id``.
    """

    request_id: str
    session_id: str
    capability_id: str
    scope: str | None = None
    context: str = ""
    timeout_s: float = 300.0


class PermissionResponseParams(_StrictModel):
    """Client → server RPC carrying the user's approval decision."""

    request_id: str
    session_id: str
    capability_id: str
    decision: Literal["allow_once", "allow_always", "deny"]


class PermissionResponseResult(_StrictModel):
    request_id: str
    resolved: bool


# Tier-C+ of 2026-05-10 memory-observability design — initial-state RPC.
# A wire client (TUI / IDE / dashboard) calls METHOD_MEMORY_STATUS on
# connect to seed its memory panel before the first MemoryWriteEvent
# fires.


class MemoryStatusParams(_StrictModel):
    """No params — the server resolves the active profile and reports both
    declarative-memory files (MEMORY.md + USER.md) for that profile.

    Per-profile selection deferred to v1.1 (matches the rest of the wire
    surface — see the ``v1: always default profile`` comments in
    :mod:`opencomputer.gateway.wire_server`).
    """


class MemoryStatusEntry(_StrictModel):
    """One file's cap status. Mirrors :class:`opencomputer.agent.memory_cap.CapStatus`
    plus a ``target`` discriminator so the client can render multiple files
    in one panel without a parallel keying scheme.

    ``pct`` is a fraction (0.0-1.0+, may exceed 1.0 mid-compaction); the
    client formats as percentage. ``paragraph_count`` is the live entry
    count (excluding the compaction header) — useful for "you have N
    durable rules" affordances.
    """

    target: str            # "MEMORY.md" | "USER.md"
    content_size: int      # bytes used
    cap_limit: int         # 4000 / 2000
    pct: float             # bytes_used / cap_limit (defensive: 0.0 if cap_limit==0)
    paragraph_count: int   # live entries, compaction header excluded


class MemoryStatusResult(_StrictModel):
    """Snapshot of memory cap status for every declarative-memory file.

    Order is stable: alphabetical by ``target`` so clients can rely on
    indexing (MEMORY.md before USER.md). Empty tuple if the active
    profile has no memory manager (e.g. minimal test harnesses) — the
    client must handle this case rather than assume non-empty.
    """

    entries: tuple[MemoryStatusEntry, ...]


# 2026-05-11 — self-evolution loop status RPC.


class EvolutionStatusDefaults(_StrictModel):
    """Module-level defaults bundled in :class:`EvolutionStatusResult`
    so the client can render the tuning panel as deltas from defaults
    without re-fetching the orchestrator's constants.
    """

    confidence_threshold: int
    dreaming_v2_score_threshold: float
    dreaming_v2_min_recall: int


class EvolutionStatusParams(_StrictModel):
    """No params — single global tuning state per profile."""


class EvolutionStatusResult(_StrictModel):
    """Snapshot of :class:`EvolutionOrchestrator` state.

    Field semantics mirror
    :class:`opencomputer.agent.evolution_orchestrator.EvolutionTuning`
    plus a wire-only ``defaults`` block so the client can render
    deltas without a second RPC.
    """

    confidence_threshold: int  # 50..95
    dreaming_v2_score_threshold: float  # 0.40..0.90
    dreaming_v2_min_recall: int  # 1..5
    decisions_observed: int  # cumulative; survives across processes
    last_recompute_ts: float  # unix seconds; 0.0 when never recomputed
    schema_version: int  # persisted-file schema version (currently 2)
    defaults: EvolutionStatusDefaults


# Map method name → (params schema, result schema). Wire dispatchers can
# look this up to validate both directions of any RPC call.
METHOD_SCHEMAS: dict[str, tuple[type[_StrictModel], type[_StrictModel]]] = {
    METHOD_HELLO: (HelloParams, HelloResult),
    METHOD_CHAT: (ChatParams, ChatResult),
    METHOD_SESSION_LIST: (SessionListParams, SessionListResult),
    METHOD_SEARCH: (SearchParams, SearchResult),
    METHOD_SKILLS_LIST: (SkillsListParams, SkillsListResult),
    METHOD_STEER_SUBMIT: (SteerSubmitParams, SteerSubmitResult),
    METHOD_SLASH_LIST: (SlashListParams, SlashListResult),
    METHOD_SLASH_DISPATCH: (SlashDispatchParams, SlashDispatchResult),
    METHOD_PERMISSION_RESPONSE: (PermissionResponseParams, PermissionResponseResult),
    METHOD_MEMORY_STATUS: (MemoryStatusParams, MemoryStatusResult),
    METHOD_EVOLUTION_STATUS: (EvolutionStatusParams, EvolutionStatusResult),
}


# ─── Per-event payload schemas ─────────────────────────────────────────


class TurnBeginPayload(_StrictModel):
    session_id: str
    user_message: str


class TurnEndPayload(_StrictModel):
    session_id: str
    iterations: int
    input_tokens: int
    output_tokens: int


class ToolCallPayload(_StrictModel):
    tool_call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultPayload(_StrictModel):
    tool_call_id: str
    content: str
    is_error: bool = False


class AssistantMessagePayload(_StrictModel):
    """Sent on stream_complete — full or delta depending on `kind`."""

    text: str
    kind: Literal["delta", "final"] = "delta"


class ErrorPayload(_StrictModel):
    error: str
    detail: str = ""


class EvolutionTuningChangedPayload(_StrictModel):
    """Server → client event on every :class:`EvolutionOrchestrator`
    tuning recompute.

    Same fields as :class:`EvolutionStatusResult` minus the persisted
    metadata (no schema_version, no last_recompute_ts — those are
    initial-state RPC concerns), plus a ``changed`` boolean so the
    client can skip rendering no-op refreshes.
    """

    confidence_threshold: int
    dreaming_v2_score_threshold: float
    dreaming_v2_min_recall: int
    decisions_observed: int
    changed: bool


class MemoryWritePayload(_StrictModel):
    """Server → client event when the agent writes to declarative memory.

    Tier-C of 2026-05-10 memory-observability design — surfaces the
    in-process ``MemoryWriteEvent`` over the wire so a TUI / IDE /
    dashboard client can render a memory-status panel and react to
    silent compaction in real time.

    Field semantics mirror :class:`plugin_sdk.ingestion.MemoryWriteEvent`
    plus a wire-only ``cap_limit`` so a panel can render the percentage
    without round-tripping a config call to learn the cap.

    No ``session_id`` field — memory writes are per-process, not per-session
    (``MemoryWriteEvent.session_id`` is always ``None`` from the publisher
    at ``opencomputer/agent/memory.py:435``). The bridge therefore
    broadcasts to every connected WS client; per-session ring-buffer
    replay does NOT cover memory.write events.
    """

    action: str  # "append" | "replace" | "remove"
    target: str  # "MEMORY.md" | "USER.md"
    content_size: int  # post-write byte count
    cap_limit: int  # 4000 for MEMORY.md, 2000 for USER.md
    compaction_delta: int = 0  # bytes freed by silent compaction (0 if none)
    dropped_paragraphs: int = 0  # paragraphs dropped by compaction (0 if none)


class StreamRetryPayload(_StrictModel):
    """Server → client event during pre-first-byte streaming retry.

    Surfaces :class:`opencomputer.agent.stream_retry.RetryStatus` over
    the wire so WS clients (TUI / IDE / dashboard) can render the same
    transient yellow banner the CLI renderer shows
    ("upstream overloaded — retry 2/4 in 1.3s") instead of staring at
    a frozen spinner.

    Field semantics mirror :class:`opencomputer.agent.stream_retry.RetryStatus`
    plus a wire-only ``request_id`` so clients tracking multiple
    concurrent turns can route the banner to the correct pane.

    Fires twice in a typical recovery:
      * once after attempt N fails (``exhausted=False``, ``delay_seconds`` > 0);
      * if all attempts exhaust, once more (``exhausted=True``,
        ``delay_seconds=0``) immediately before the wrapper re-raises.
    """

    request_id: str
    attempt: int
    next_attempt: int
    max_attempts: int
    delay_seconds: float
    error_kind: str  # "overloaded" | "bad_gateway" | "timeout" | ...
    error_message: str  # truncated str(exc), ≤ 200 chars
    exhausted: bool


class ProfileSwapPayload(_StrictModel):
    """Server → client event when the agent swaps the active profile.

    Carries the minimum a client UI needs to render
    ``"↪ from_profile → to_profile (handoff)"`` and refresh any
    profile-bound surfaces (memory panel, MCP list, plugin list).

    No ``session_id`` keying — profile is per-process state. The bridge
    broadcasts globally; clients re-fetch profile-bound state via
    existing RPCs (``memory.status``, plugin manifests, etc.).
    """

    from_profile: str
    to_profile: str
    trigger: str  # "auto" | "manual" | "cli"
    classifier_confidence: float = 0.0  # 0 for non-auto triggers
    classifier_reason: str = ""
    has_handoff: bool = False  # True if a handoff document was written


# Map event name → payload schema.
EVENT_SCHEMAS: dict[str, type[_StrictModel]] = {
    EVENT_TURN_BEGIN: TurnBeginPayload,
    EVENT_TURN_END: TurnEndPayload,
    EVENT_TOOL_CALL: ToolCallPayload,
    EVENT_TOOL_RESULT: ToolResultPayload,
    EVENT_ASSISTANT_MESSAGE: AssistantMessagePayload,
    EVENT_ERROR: ErrorPayload,
    EVENT_PERMISSION_REQUEST: PermissionRequestPayload,
    EVENT_MEMORY_WRITE: MemoryWritePayload,
    EVENT_EVOLUTION_TUNING_CHANGED: EvolutionTuningChangedPayload,
    EVENT_STREAM_RETRY: StreamRetryPayload,
    EVENT_PROFILE_SWAP: ProfileSwapPayload,
}


__all__ = [
    # v1 re-exports
    "WireRequest",
    "WireResponse",
    "WireEvent",
    "METHOD_HELLO",
    "METHOD_CHAT",
    "METHOD_SESSION_LIST",
    "METHOD_SEARCH",
    "METHOD_SKILLS_LIST",
    "METHOD_STEER_SUBMIT",
    "METHOD_SLASH_LIST",
    "METHOD_SLASH_DISPATCH",
    "METHOD_PERMISSION_RESPONSE",
    "METHOD_MEMORY_STATUS",
    "METHOD_EVOLUTION_STATUS",
    "SlashListParams",
    "SlashListResult",
    "SlashCommandInfo",
    "SlashDispatchParams",
    "SlashDispatchResult",
    "EVENT_TURN_BEGIN",
    "EVENT_TURN_END",
    "EVENT_TOOL_CALL",
    "EVENT_TOOL_RESULT",
    "EVENT_ASSISTANT_MESSAGE",
    "EVENT_ERROR",
    "EVENT_PERMISSION_REQUEST",
    "EVENT_MEMORY_WRITE",
    "EVENT_EVOLUTION_TUNING_CHANGED",
    "EVENT_STREAM_RETRY",
    "EVENT_PROFILE_SWAP",
    "ProfileSwapPayload",
    # v2 method schemas
    "HelloParams",
    "HelloResult",
    "ChatParams",
    "ChatResult",
    "SessionListParams",
    "SessionListResult",
    "SearchParams",
    "SearchResult",
    "SkillsListParams",
    "SkillsListResult",
    "SteerSubmitParams",
    "SteerSubmitResult",
    "PermissionRequestPayload",
    "PermissionResponseParams",
    "PermissionResponseResult",
    "MemoryStatusParams",
    "MemoryStatusEntry",
    "MemoryStatusResult",
    "EvolutionStatusParams",
    "EvolutionStatusResult",
    "EvolutionStatusDefaults",
    "METHOD_SCHEMAS",
    # v2 event schemas
    "TurnBeginPayload",
    "TurnEndPayload",
    "ToolCallPayload",
    "ToolResultPayload",
    "AssistantMessagePayload",
    "ErrorPayload",
    "MemoryWritePayload",
    "EvolutionTuningChangedPayload",
    "StreamRetryPayload",
    "EVENT_SCHEMAS",
]
