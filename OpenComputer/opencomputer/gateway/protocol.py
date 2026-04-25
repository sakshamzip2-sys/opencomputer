"""
Gateway wire protocol — JSON over WebSocket.

Clients connect to the gateway and exchange messages in this format.
Openclaw-style: typed schemas, request/response/event messages.

Three message shapes:
    req   — client → gateway request            {type:"req", id, method, params}
    res   — gateway → client response           {type:"res", id, ok, payload?, error?}
    event — gateway → client server-push event  {type:"event", event, payload}
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ─── Request ────────────────────────────────────────────────────


class WireRequest(BaseModel):
    type: Literal["req"] = "req"
    id: str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


# ─── Response ───────────────────────────────────────────────────


class WireResponse(BaseModel):
    type: Literal["res"] = "res"
    id: str
    ok: bool
    payload: dict[str, Any] | None = None
    error: str | None = None


# ─── Events (server-push) ───────────────────────────────────────


class WireEvent(BaseModel):
    type: Literal["event"] = "event"
    event: str  # e.g. "turn.begin", "tool.call", "assistant.delta", "turn.end"
    payload: dict[str, Any] = Field(default_factory=dict)


# ─── Method names (client → gateway) ────────────────────────────

# These are the RPC methods clients can call.
METHOD_HELLO = "hello"  # handshake, exchange capabilities
METHOD_CHAT = "chat"  # send a user message, get assistant response
METHOD_SESSION_LIST = "sessions.list"
METHOD_SEARCH = "search"
METHOD_SKILLS_LIST = "skills.list"
METHOD_STEER_SUBMIT = "steer.submit"  # P-2 round 2a: mid-run /steer nudge


# ─── Event names (gateway → client) ─────────────────────────────

EVENT_TURN_BEGIN = "turn.begin"
EVENT_TURN_END = "turn.end"
EVENT_TOOL_CALL = "tool.call"
EVENT_TOOL_RESULT = "tool.result"
EVENT_ASSISTANT_MESSAGE = "assistant.message"
EVENT_ERROR = "error"


__all__ = [
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
]
