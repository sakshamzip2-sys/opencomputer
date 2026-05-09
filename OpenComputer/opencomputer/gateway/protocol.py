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
    # Sub-project G (openclaw-parity) Task 9 - programmable error
    # category. Optional; old clients ignore. Value mirrors
    # opencomputer.gateway.error_codes.ErrorCode (snake_case strings)
    # so the wire shape is stable even if the enum gains new codes.
    code: str | None = None


# ─── Events (server-push) ───────────────────────────────────────


class WireEvent(BaseModel):
    type: Literal["event"] = "event"
    event: str  # e.g. "turn.begin", "tool.call", "assistant.delta", "turn.end"
    payload: dict[str, Any] = Field(default_factory=dict)
    # v1.1 plan-1 M3.3 (2026-05-09) — monotonic per-session sequence number
    # for wire-reconnect replay. Optional + default None so old clients that
    # built/decoded events without this field still work.
    seq: int | None = None


# ─── Method names (client → gateway) ────────────────────────────

# These are the RPC methods clients can call.
METHOD_HELLO = "hello"  # handshake, exchange capabilities
METHOD_CHAT = "chat"  # send a user message, get assistant response
METHOD_SESSION_LIST = "sessions.list"
METHOD_SEARCH = "search"
METHOD_SKILLS_LIST = "skills.list"
METHOD_STEER_SUBMIT = "steer.submit"  # P-2 round 2a: mid-run /steer nudge
METHOD_SLASH_LIST = "slash.list"  # 2026-05-07: enumerate slash commands
METHOD_SLASH_DISPATCH = "slash.dispatch"  # 2026-05-07: invoke a slash command
# v1.1 plan-1 M3.1 (2026-05-09) — permission resolution RPC. Wire clients
# call this in response to a permission.request event to allow/deny a
# Tier-2 capability on behalf of the user.
METHOD_PERMISSION_RESPONSE = "permission.response"


# ─── Event names (gateway → client) ─────────────────────────────

EVENT_TURN_BEGIN = "turn.begin"
EVENT_TURN_END = "turn.end"
EVENT_TOOL_CALL = "tool.call"
EVENT_TOOL_RESULT = "tool.result"
EVENT_ASSISTANT_MESSAGE = "assistant.message"
EVENT_ERROR = "error"
# v1.1 plan-1 M3.1 (2026-05-09) — Tier-2 consent gate fires this event
# on a wire-bound session when no channel adapter is available. The
# client responds via the METHOD_PERMISSION_RESPONSE RPC.
EVENT_PERMISSION_REQUEST = "permission.request"


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
    "METHOD_SLASH_LIST",
    "METHOD_SLASH_DISPATCH",
    "METHOD_PERMISSION_RESPONSE",
    "EVENT_TURN_BEGIN",
    "EVENT_TURN_END",
    "EVENT_TOOL_CALL",
    "EVENT_TOOL_RESULT",
    "EVENT_ASSISTANT_MESSAGE",
    "EVENT_ERROR",
    "EVENT_PERMISSION_REQUEST",
]
