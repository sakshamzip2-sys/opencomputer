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
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    EVENT_TURN_BEGIN,
    EVENT_TURN_END,
    METHOD_CHAT,
    METHOD_HELLO,
    METHOD_SEARCH,
    METHOD_SESSION_LIST,
    METHOD_SKILLS_LIST,
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
    """Client capability declaration sent on connect."""

    client: str  # e.g. "opencomputer-tui/0.1.0"
    capabilities: tuple[str, ...] = ()


class HelloResult(_StrictModel):
    """Gateway capability response."""

    server: str  # e.g. "opencomputer/0.1.0"
    capabilities: tuple[str, ...]
    protocol_version: int = 2


class ChatParams(_StrictModel):
    """Send a user message; receive an assistant response (synchronous)."""

    message: str
    session_id: str | None = None
    plan_mode: bool = False


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


# Map method name → (params schema, result schema). Wire dispatchers can
# look this up to validate both directions of any RPC call.
METHOD_SCHEMAS: dict[str, tuple[type[_StrictModel], type[_StrictModel]]] = {
    METHOD_HELLO: (HelloParams, HelloResult),
    METHOD_CHAT: (ChatParams, ChatResult),
    METHOD_SESSION_LIST: (SessionListParams, SessionListResult),
    METHOD_SEARCH: (SearchParams, SearchResult),
    METHOD_SKILLS_LIST: (SkillsListParams, SkillsListResult),
    METHOD_STEER_SUBMIT: (SteerSubmitParams, SteerSubmitResult),
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
    "EVENT_TURN_BEGIN",
    "EVENT_TURN_END",
    "EVENT_TOOL_CALL",
    "EVENT_TOOL_RESULT",
    "EVENT_ASSISTANT_MESSAGE",
    "EVENT_ERROR",
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
