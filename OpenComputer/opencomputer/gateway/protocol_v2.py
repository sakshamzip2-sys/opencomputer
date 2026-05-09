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
    EVENT_PERMISSION_REQUEST,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    EVENT_TURN_BEGIN,
    EVENT_TURN_END,
    METHOD_CHAT,
    METHOD_HELLO,
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


# Map event name → payload schema.
EVENT_SCHEMAS: dict[str, type[_StrictModel]] = {
    EVENT_TURN_BEGIN: TurnBeginPayload,
    EVENT_TURN_END: TurnEndPayload,
    EVENT_TOOL_CALL: ToolCallPayload,
    EVENT_TOOL_RESULT: ToolResultPayload,
    EVENT_ASSISTANT_MESSAGE: AssistantMessagePayload,
    EVENT_ERROR: ErrorPayload,
    EVENT_PERMISSION_REQUEST: PermissionRequestPayload,
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
    "METHOD_SCHEMAS",
    # v2 event schemas
    "TurnBeginPayload",
    "TurnEndPayload",
    "ToolCallPayload",
    "ToolResultPayload",
    "AssistantMessagePayload",
    "ErrorPayload",
    "EVENT_SCHEMAS",
]
