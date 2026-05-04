"""
Hook primitives — for plugins that want to intercept lifecycle events.

Hooks are fire-and-forget event handlers. Critical rule (from kimi-cli):
post-action hooks MUST NOT block the agent loop. Use async + let the
loop move on. The hook engine discards exceptions silently (logs them).

Available events:
    PreToolUse              — fires before any tool runs (can approve/block/modify)
    PostToolUse             — fires after any tool runs (log, inspect result)
    Stop                    — fires when the model stops asking for tools (can force continue)
    SessionStart            — fires once when a new conversation begins
    SessionEnd              — fires when conversation ends
    UserPromptSubmit        — fires when the user submits a message
    PreCompact              — fires immediately before CompactionEngine summarises
    SubagentStop            — fires when a delegated subagent finishes its run
    Notification            — fires when PushNotification is dispatched (one per channel)
    PreLLMCall              — fires immediately before each provider.complete() call
    PostLLMCall             — fires immediately after each provider.complete() call
    TransformToolResult     — wraps a tool result (modified_message replaces it)
    TransformTerminalOutput — wraps a streaming bash chunk (modified_message replaces it)
    BeforePromptBuild       — fires before PromptBuilder renders the system prompt
    BeforeCompaction        — fires before compaction summarises (sees the message slice)
    AfterCompaction         — fires after compaction completes (sees the new message list)
    BeforeMessageWrite      — fires before SessionDB persists a message

Hook ordering: handlers can declare ``priority`` on their HookSpec — lower
priorities run first; FIFO within the same priority bucket. The default is 100,
so hooks registered before the priority field existed continue to run in
registration order.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from plugin_sdk.core import Message, ToolCall, ToolResult
from plugin_sdk.runtime_context import RuntimeContext


class HookEvent(str, Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_COMPACT = "PreCompact"
    SUBAGENT_STOP = "SubagentStop"
    NOTIFICATION = "Notification"
    # Round 2A P-1 expansion — additional lifecycle hooks. Declaration order
    # matters: ALL_HOOK_EVENTS preserves it so plugins iterating the tuple see
    # the original events first, then these eight in this order.
    PRE_LLM_CALL = "PreLLMCall"
    POST_LLM_CALL = "PostLLMCall"
    TRANSFORM_TOOL_RESULT = "TransformToolResult"
    TRANSFORM_TERMINAL_OUTPUT = "TransformTerminalOutput"
    BEFORE_PROMPT_BUILD = "BeforePromptBuild"
    BEFORE_COMPACTION = "BeforeCompaction"
    AFTER_COMPACTION = "AfterCompaction"
    BEFORE_MESSAGE_WRITE = "BeforeMessageWrite"


@dataclass(frozen=True, slots=True)
class HookContext:
    """Data passed to every hook invocation. Read-only."""

    event: HookEvent
    session_id: str
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    message: Message | None = None
    #: Runtime flags for this invocation (plan_mode, yolo_mode, custom).
    #: None for backwards compatibility with hooks written before this field.
    runtime: RuntimeContext | None = None
    # Round 2A P-1 expansion — optional fields per new hook event. All default
    # to ``None`` so existing HookContext callers (Pre/PostToolUse, Stop, etc.)
    # work unchanged.
    #:
    #: Rendered system prompt text — populated for BEFORE_PROMPT_BUILD.
    prompt_text: str | None = None
    #: Snapshot of conversation messages — populated for PRE/POST_LLM_CALL and
    #: BEFORE/AFTER_COMPACTION. Typed as ``list[Any]`` so the SDK doesn't need
    #: to re-import :class:`~plugin_sdk.core.Message` from a wider scope; the
    #: value is conventionally ``list[Message]``.
    messages: list[Any] | None = None
    #: Single streamed chunk of text — populated for TRANSFORM_TERMINAL_OUTPUT.
    streamed_chunk: str | None = None
    #: Provider model name in flight — populated for PRE/POST_LLM_CALL.
    model: str | None = None


@dataclass(frozen=True, slots=True)
class HookDecision:
    """A hook's response. PreToolUse hooks use `decision` to approve/block."""

    decision: Literal["approve", "block", "pass"] = "pass"
    reason: str = ""
    modified_message: str = ""  # if set, injected as a system reminder


# Hook handler is an async callable: (ctx) -> HookDecision or None (= "pass")
HookHandler = Callable[[HookContext], Awaitable[HookDecision | None]]


@dataclass(frozen=True, slots=True)
class HookSpec:
    """What plugins register — one event + one handler + an optional matcher."""

    event: HookEvent
    handler: HookHandler
    matcher: str | None = None  # regex over tool names for PreToolUse/PostToolUse
    fire_and_forget: bool = True  # true for post-action hooks
    #: Round 2A P-1: lower priority runs first; FIFO within the same bucket.
    #: Default 100 keeps hooks registered before this field was introduced
    #: running in their original (FIFO) order. Same-priority FIFO is enforced
    #: by the engine, which uses the registration index as a secondary sort
    #: key so the spec itself stays immutable.
    priority: int = 100
    #: Per-hook timeout in milliseconds. None or 0 = no timeout (current
    #: behaviour). When the handler exceeds this, the engine logs a warning
    #: and treats it as 'pass' (fail-open), matching OC's existing hook
    #: contract (CLAUDE.md §7: a wedged hook must never wedge the loop).
    #: Mirrors openclaw's plugins.entries.<id>.hooks.timeoutMs.
    timeout_ms: int | None = None


__all__ = [
    "HookEvent",
    "HookContext",
    "HookDecision",
    "HookHandler",
    "HookSpec",
    "ALL_HOOK_EVENTS",
]

#: All HookEvent values, in declaration order. Useful for plugins that want to
#: register a single handler against every lifecycle event (audit logging etc.).
ALL_HOOK_EVENTS: tuple[HookEvent, ...] = (
    # Original 9 events — order preserved for backwards compat with iterators
    # that depend on a specific position.
    HookEvent.PRE_TOOL_USE,
    HookEvent.POST_TOOL_USE,
    HookEvent.STOP,
    HookEvent.SESSION_START,
    HookEvent.SESSION_END,
    HookEvent.USER_PROMPT_SUBMIT,
    HookEvent.PRE_COMPACT,
    HookEvent.SUBAGENT_STOP,
    HookEvent.NOTIFICATION,
    # Round 2A P-1 — 8 new events, declaration order matches the docstring.
    HookEvent.PRE_LLM_CALL,
    HookEvent.POST_LLM_CALL,
    HookEvent.TRANSFORM_TOOL_RESULT,
    HookEvent.TRANSFORM_TERMINAL_OUTPUT,
    HookEvent.BEFORE_PROMPT_BUILD,
    HookEvent.BEFORE_COMPACTION,
    HookEvent.AFTER_COMPACTION,
    HookEvent.BEFORE_MESSAGE_WRITE,
)
