"""
Hook primitives — for plugins that want to intercept lifecycle events.

Hooks are fire-and-forget event handlers. Critical rule (from kimi-cli):
post-action hooks MUST NOT block the agent loop. Use async + let the
loop move on. The hook engine discards exceptions silently (logs them).

Available events:
    PreToolUse       — fires before any tool runs (can approve/block/modify)
    PostToolUse      — fires after any tool runs (log, inspect result)
    Stop             — fires when the model stops asking for tools (can force continue)
    SessionStart     — fires once when a new conversation begins
    SessionEnd       — fires when conversation ends
    UserPromptSubmit — fires when the user submits a message
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Awaitable, Literal

from plugin_sdk.core import Message, ToolCall, ToolResult
from plugin_sdk.runtime_context import RuntimeContext


class HookEvent(str, Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"


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


__all__ = [
    "HookEvent",
    "HookContext",
    "HookDecision",
    "HookHandler",
    "HookSpec",
]
