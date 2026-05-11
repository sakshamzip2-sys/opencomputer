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
# Tier-C+ of 2026-05-10 memory-observability design — initial-state RPC.
# A wire client (TUI / IDE / dashboard) calls this on connect to seed its
# memory panel with current MEMORY.md / USER.md cap status before the first
# write event fires. Without this, a long-idle session shows nothing in the
# memory panel until the user writes to memory; with it, the panel reflects
# the live state from the first frame.
METHOD_MEMORY_STATUS = "memory.status"


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
# Tier-C of 2026-05-10 memory-observability design — broadcasts every
# MemoryWriteEvent the in-process bus publishes, so a TUI / IDE / dashboard
# client can render a memory-status panel and surface silent compaction in
# real time. No session_id keying — broadcast to all connected WS clients.
EVENT_MEMORY_WRITE = "memory.write"

# 2026-05-11 — self-evolution loop wire surface. Broadcasts every
# EvolutionTuningChangedEvent so dashboards / TUI / IDE clients can
# render a "tuning changed" toast or refresh a tuning panel without
# polling the persisted JSON file. No session_id keying — broadcast
# to all clients (the tuning state is per-process, not per-session).
EVENT_EVOLUTION_TUNING_CHANGED = "evolution.tuning_changed"

# 2026-05-11 — pre-first-byte transient-retry surface for streaming
# turns. Mirrors the CLI renderer's yellow retry panel for any WS
# client (TUI, IDE, dashboard). Keyed to the active turn's request_id
# so a client tracking multiple sessions can route the toast to the
# correct pane. Fires between attempts (exhausted=False) and on the
# final attempt's failure (exhausted=True). Replay is per-session via
# the existing _session_rings, so a brief disconnect doesn't drop the
# status update.
EVENT_STREAM_RETRY = "stream.retry"

# Initial-state RPC companion to EVENT_EVOLUTION_TUNING_CHANGED — lets a
# freshly-connecting client fetch the current tuning without waiting
# for the next change event. Same shape as the broadcast payload.
METHOD_EVOLUTION_STATUS = "evolution.status"


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
    "METHOD_MEMORY_STATUS",
    "METHOD_EVOLUTION_STATUS",
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
]
