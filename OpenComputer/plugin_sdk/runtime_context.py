"""
RuntimeContext — per-turn flags passed through the agent loop.

The CLI / caller builds this once per invocation. The agent loop passes it
to InjectionProviders (so they can decide whether to fire) and to Hooks
(so they can decide whether to block). `delegate` propagates it to
subagents, so modes like `--plan` apply to the whole subagent tree.

Frozen dataclass — safe to share across tasks / threads.

``RequestContext`` (Task I.9) is the adjacent per-REQUEST scope —
populated by the gateway around each inbound channel message so plugins
can query the request identity (auth gating, rate limiting, activation
context queries). CLI + direct AgentLoop calls leave it None.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Flags that cross-cutting modes read. Immutable per invocation."""

    #: Plan mode — agent should describe what it would do without executing
    #: destructive tools. Enforced both via injection (prompt) and hook (hard-block).
    plan_mode: bool = False

    #: Yolo mode — auto-approve dangerous operations. Mutually exclusive with plan_mode
    #: (we enforce this in the CLI; if both set, plan_mode wins).
    yolo_mode: bool = False

    #: Which agent context this invocation belongs to. ``Literal`` narrows the
    #: allowed values at type-check time so typos like ``"Cron"`` fail mypy
    #: rather than silently no-op-ing the guards downstream. Default
    #: ``"chat"`` preserves existing behaviour. ``"cron"`` and ``"flush"``
    #: short-circuit external memory providers — those batch jobs shouldn't
    #: spin a Docker stack for quick background work (baseline SQLite+FTS5 is
    #: enough). Mirrors Hermes'
    #: ``sources/hermes-agent/plugins/memory/honcho/__init__.py:279-286``.
    agent_context: Literal["chat", "cron", "flush", "review"] = "chat"

    #: Escape hatch for third-party plugins to add their own modes without
    #: forcing an SDK version bump.
    custom: dict[str, Any] = field(default_factory=dict)

    delegation_depth: int = 0
    """How deep we are in the delegation chain. 0 = parent (top of stack).
    1 = child of a delegated call. Each `DelegateTool.execute` increments this
    for the child runtime. Used by `DelegateTool` to enforce
    `LoopConfig.max_delegation_depth` (default 2 = parent → child →
    grandchild rejected). Mirrors Hermes `MAX_DEPTH` from
    `sources/hermes-agent/tools/delegate_tool.py`."""


#: A sentinel "no flags" default — useful when callers don't care about modes.
DEFAULT_RUNTIME_CONTEXT = RuntimeContext()


# ─── RequestContext (Task I.9) ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Per-request metadata available to plugins during a dispatch.

    Populated by the gateway around each inbound ``MessageEvent`` and
    around each wire-server call. CLI + direct ``AgentLoop.run_conversation``
    callers leave it ``None`` — ``PluginAPI.request_context`` returns
    ``None`` when no scope is active.

    Plugins read this via ``PluginAPI.request_context`` inside any code
    that runs during dispatch (tool handlers, injection providers,
    hooks). Use cases:

    * **Auth gating** — block a tool for users outside a channel allowlist.
    * **Rate limiting** — key a token-bucket on ``(channel, user_id)``.
    * **Activation-context queries** — "am I being called from Telegram
      or from the CLI?" (matches OpenClaw's per-request plugin scope at
      ``sources/openclaw/src/gateway/server-plugins.ts:47-64, 107-144``).

    Immutable — the gateway assembles the ctx once per request and
    never mutates it. A new request gets a fresh ``RequestContext``.
    """

    #: UUID per incoming request. Never reused — a long-running wire
    #: connection will cycle through many request_ids, one per method call.
    request_id: str

    #: Channel identifier ("telegram", "discord", "wire", "cli", ...).
    #: ``None`` if the dispatch path does not know (shouldn't happen in
    #: practice, but defensive default).
    channel: str | None = None

    #: Channel-specific user identifier. For Telegram / Discord this is
    #: the chat_id; for wire server it's the connection id. Plugins
    #: that rate-limit or auth-gate key on this.
    user_id: str | None = None

    #: Agent session id — sha256 of ``(platform, chat_id)`` in the
    #: current dispatcher. Same session across turns in the same chat.
    session_id: str | None = None

    #: ``time.monotonic()`` reading at request start. Used by request
    #: timing / rate-limit token buckets. ``0.0`` default so callers
    #: that don't care can skip the argument.
    started_at: float = 0.0


__all__ = ["RuntimeContext", "DEFAULT_RUNTIME_CONTEXT", "RequestContext"]
