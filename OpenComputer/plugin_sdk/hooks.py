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
    BeforeTask              — fires after UserPromptSubmit, before first LLM
                              call (blocking; lets handlers inject context)
    BeforeInstall           — fires after extract + scan, before plugin activates an install
    BeforeModelResolve      — fires before model_resolver.resolve_model() runs
    MessageSending          — fires before a channel adapter sends an outgoing message
    MessageSent             — fires after a channel adapter sends an outgoing message

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
    # Wave 5 T13/T14 — Hermes-port hook events (1ef1e4c66 + 30307a980).
    PRE_GATEWAY_DISPATCH = "PreGatewayDispatch"
    PRE_APPROVAL_REQUEST = "PreApprovalRequest"
    POST_APPROVAL_RESPONSE = "PostApprovalResponse"
    # Social-traces plugin — fires after USER_PROMPT_SUBMIT and before the
    # first LLM call. Blocking: handlers may return HookDecision with
    # ``modified_message`` set to inject a <system-reminder> user message
    # (used by the social-traces plugin to inject pre-fetched TraceCards
    # into context). See docs/plans/social-traces-plugin.md §8.
    BEFORE_TASK = "BeforeTask"
    # 2026-05-06 — install lifecycle (S3 leftover from OpenClaw deep-comparison).
    BEFORE_INSTALL = "BeforeInstall"
    # 2026-05-06 — Phase 3 (S3 leftovers from OpenClaw deep-comparison).
    BEFORE_MODEL_RESOLVE = "BeforeModelResolve"
    MESSAGE_SENDING = "MessageSending"
    MESSAGE_SENT = "MessageSent"
    # 2026-05-08 — Hermes Doc-2 parity (see
    # docs/refs/hermes-agent/2026-05-08-kanban-goals-execcode-hooks-parity.md).
    # SESSION_END fires per ``run_conversation`` (every turn). SESSION_FINALIZE
    # fires once when the surface tears down a session entirely (CLI exits,
    # gateway evicts, websocket closes). Last chance to flush state.
    SESSION_FINALIZE = "SessionFinalize"
    # SESSION_RESET fires after ``/new`` / ``/reset`` / ``/clear`` allocates a
    # fresh session id. Distinct from SESSION_START because the previous
    # session id is also exposed via ``HookContext.previous_session_id`` so a
    # plugin can carry forward state. Gateway + CLI both fire this.
    SESSION_RESET = "SessionReset"
    # TRANSFORM_LLM_OUTPUT fires once per turn after the final assistant text
    # is assembled, before delivery to channel/console. Handlers may return
    # ``HookDecision(decision="rewrite", rewritten_text=...)`` to replace the
    # response. First non-empty rewrite wins; later handlers see the rewritten
    # text. Use cases: PII redaction post-LLM, tone adjustments, citation
    # appending, A/B response routing.
    TRANSFORM_LLM_OUTPUT = "TransformLlmOutput"

    # CC §2 (2026-05-11) — five additional Claude Code lifecycle events.
    # Each is fire-and-forget by default (observers, not gates).

    # POST_TOOL_BATCH fires once after a parallel tool batch resolves (all
    # ``dispatch_tool_calls`` results are in). Use case: telemetry that wants
    # the FULL batch view rather than N separate POST_TOOL_USE fires. The
    # context carries ``batch_results`` (list of ToolResult) and
    # ``batch_calls`` (list of ToolCall) in parallel order.
    POST_TOOL_BATCH = "PostToolBatch"

    # USER_PROMPT_EXPANSION fires when a slash command expands into a prompt
    # (e.g. ``/scrape <url>`` → an injected user message). Distinct from
    # ``UserPromptSubmit`` which fires for raw user text. The context's
    # ``expansion_source`` carries the slash command name and ``message`` /
    # ``prompt_text`` carry the expanded text. Use case: audit which slashes
    # generate prompts vs return immediately.
    USER_PROMPT_EXPANSION = "UserPromptExpansion"

    # INSTRUCTIONS_LOADED fires when a ``CLAUDE.md`` / ``OPENCOMPUTER.md`` /
    # ``SOUL.md`` / ``AGENTS.md`` instruction file is loaded into the
    # system prompt. The context's ``instructions_path`` carries the file
    # path; ``prompt_text`` carries the file body. Use case: react to
    # project-specific rules (e.g. enable strict-mode tools when a
    # given file is present).
    INSTRUCTIONS_LOADED = "InstructionsLoaded"

    # CWD_CHANGED fires when the agent's working directory changes between
    # turns. The context carries the new ``cwd`` and the previous
    # ``previous_cwd``. Use case: re-load directory-scoped instructions or
    # run direnv. Fires only when the cwd actually differs from the
    # previous turn's cwd — no fire on first-turn cwd capture.
    CWD_CHANGED = "CwdChanged"

    # FILE_CHANGED fires when a watched file is observed to change on disk.
    # The file-watcher itself is plugin-provided (no core watcher daemon);
    # this event provides the contract so plugins can register watchers and
    # fan out changes via the standard hook surface. Context carries
    # ``file_path`` and the change ``kind`` ("created" | "modified" |
    # "deleted").
    FILE_CHANGED = "FileChanged"


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
    # Wave 5 T13/T14/T15 — Hermes-port additional fields.
    #: Inbound gateway message text — populated for PRE_GATEWAY_DISPATCH.
    gateway_event_text: str | None = None
    #: Sender identifier (channel-specific) — populated for PRE_GATEWAY_DISPATCH.
    sender_id: str | None = None
    #: Approval surface — "cli" or "gateway" — for PRE/POST_APPROVAL_*.
    surface: str | None = None
    #: Command being approved (PRE/POST_APPROVAL_*).
    command: str | None = None
    #: User choice on POST_APPROVAL_RESPONSE — once|session|always|deny|timeout.
    choice: str | None = None
    #: Tool dispatch latency in ms — populated for POST_TOOL_USE +
    #: TRANSFORM_TOOL_RESULT (Wave 5 T15 — Hermes 59b56d445).
    duration_ms: int | None = None
    # 2026-05-06 — install lifecycle context fields. Populated only for
    # BEFORE_INSTALL; default None so existing HookContext callers across
    # the loop / gateway / approval paths stay unchanged.
    #:
    #: Install source: "catalog" | "git" | "url" | "path".
    install_source: str | None = None
    #: Raw URL the user typed (or slug for catalog, abs path for path).
    install_url: str | None = None
    #: Resolved plugin id (post-extract, post-manifest-parse).
    install_plugin_id: str | None = None
    #: install_security_scan.ScanReport — typed loosely as object so the SDK
    #: doesn't need to re-import it (the plugin loader is the only producer).
    install_scan_report: object | None = None
    # 2026-05-06 — Phase 3 (S3 leftovers from OpenClaw deep-comparison).
    #: Pre-resolve model alias text — populated for BEFORE_MODEL_RESOLVE.
    #: Distinct from ``model`` (which carries the post-resolve canonical id
    #: for PRE/POST_LLM_CALL). A handler may set ``modified_message`` on
    #: HookDecision to redirect resolution to a different alias key.
    pre_resolve_model: str | None = None
    #: Outgoing channel message text — populated for MESSAGE_SENDING /
    #: MESSAGE_SENT. Distinct from ``message`` (which carries inbound).
    outgoing_text: str | None = None
    #: Channel platform string ("telegram" | "discord" | "cli" | ...) —
    #: populated for MESSAGE_SENDING / MESSAGE_SENT.
    channel: str | None = None
    #: Outgoing chat id (channel-specific) — populated for MESSAGE_SENDING / MESSAGE_SENT.
    outgoing_chat_id: str | None = None
    # 2026-05-08 — Hermes Doc-2 parity additions.
    #: Reason a SESSION_FINALIZE fires — "cli_exit" | "gateway_evict" |
    #: "wire_disconnect" | "shutdown" | "error". Handlers that need to behave
    #: differently for normal vs abnormal exits can branch on this.
    finalize_reason: str | None = None
    #: Previous session id rotated out by ``/new`` / ``/reset`` / ``/clear`` —
    #: populated for SESSION_RESET so a plugin can carry forward in-memory
    #: caches keyed on the old id.
    previous_session_id: str | None = None
    #: The platform/surface the reset originated on — "cli" | "gateway" |
    #: "wire" | "acp" — populated for SESSION_RESET / SESSION_FINALIZE.
    surface_origin: str | None = None
    #: Final assistant text being delivered — populated for
    #: TRANSFORM_LLM_OUTPUT. Distinct from ``outgoing_text`` which is the
    #: per-channel-message text in MESSAGE_SENDING (multiple chunks possible).
    response_text: str | None = None
    # CC §2 (2026-05-11) — five additional events; matching context fields.
    #: Parallel-batch tool calls + results — populated for POST_TOOL_BATCH.
    #: Same length tuples; ``batch_calls[i]`` produced ``batch_results[i]``.
    batch_calls: tuple[Any, ...] | None = None
    batch_results: tuple[Any, ...] | None = None
    #: Slash command name that produced the expansion — populated for
    #: USER_PROMPT_EXPANSION. E.g. "scrape", "btw", "search".
    expansion_source: str | None = None
    #: Path of the instruction file that was just loaded — populated for
    #: INSTRUCTIONS_LOADED. ``prompt_text`` carries the body.
    instructions_path: str | None = None
    #: Current and previous working directory — populated for CWD_CHANGED.
    cwd: str | None = None
    previous_cwd: str | None = None
    #: Path of the file whose state change is being notified — populated
    #: for FILE_CHANGED. ``change_kind`` carries the verb.
    file_path: str | None = None
    change_kind: str | None = None  # "created" | "modified" | "deleted"
    #: 1-indexed turn number for the turn this event belongs to — populated
    #: for STOP (and any future per-turn event that needs it). Mirrors
    #: :attr:`plugin_sdk.injection.InjectionContext.turn_index` so a STOP
    #: handler can correlate against the turn an injection provider ran on.
    #: Default ``0`` is the "caller did not thread the counter" sentinel,
    #: keeping every existing HookContext construction valid.
    turn_index: int = 0


@dataclass(frozen=True, slots=True)
class HookDecision:
    """A hook's response. PreToolUse hooks use `decision` to approve/block."""

    # Wave 5 T13 — added "skip", "rewrite", "allow" verdicts for
    # PreGatewayDispatch (Hermes 1ef1e4c66). "skip" drops the message
    # silently, "rewrite" replaces gateway_event_text via rewritten_text,
    # "allow" is a positive ack equivalent to "pass" but documents that
    # the hook explicitly inspected and approved.
    decision: Literal[
        "approve", "block", "pass", "skip", "rewrite", "allow",
    ] = "pass"
    reason: str = ""
    modified_message: str = ""  # if set, injected as a system reminder
    #: Wave 5 T13 — for decision="rewrite", the new event text.
    rewritten_text: str | None = None
    #: 2026-05-08 G4 — text to inject into the user message for
    #: PRE_LLM_CALL only. Mirrors Hermes' shell-hook stdout
    #: ``{"context": "..."}`` shape and the existing plugin-side
    #: pre_llm_call return-value contract. Ignored for non-PRE_LLM_CALL
    #: events (callers in loop.py decide the apply-condition).
    inject_context: str | None = None


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
    # Wave 5 T13/T14
    HookEvent.PRE_GATEWAY_DISPATCH,
    HookEvent.PRE_APPROVAL_REQUEST,
    HookEvent.POST_APPROVAL_RESPONSE,
    # Social-traces plugin
    HookEvent.BEFORE_TASK,
    # 2026-05-06 — install lifecycle
    HookEvent.BEFORE_INSTALL,
    # 2026-05-06 — Phase 3 (S3 leftovers from OpenClaw deep-comparison)
    HookEvent.BEFORE_MODEL_RESOLVE,
    HookEvent.MESSAGE_SENDING,
    HookEvent.MESSAGE_SENT,
    # 2026-05-08 — Hermes Doc-2 parity additions.
    HookEvent.SESSION_FINALIZE,
    HookEvent.SESSION_RESET,
    HookEvent.TRANSFORM_LLM_OUTPUT,
    # CC §2 (2026-05-11) — five Claude-Code lifecycle additions.
    HookEvent.POST_TOOL_BATCH,
    HookEvent.USER_PROMPT_EXPANSION,
    HookEvent.INSTRUCTIONS_LOADED,
    HookEvent.CWD_CHANGED,
    HookEvent.FILE_CHANGED,
)
