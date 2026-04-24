# HookEvent catalog

Every entry in `plugin_sdk.hooks.HookEvent`, what fires it, which
`HookContext` fields are populated, and whether a `HookDecision` from
the handler changes anything.

## PreToolUse

Fires immediately before the agent loop dispatches a tool call. The
first hook to return `HookDecision(decision="block", ...)` aborts the
dispatch — the tool never runs, and the block reason is fed back to the
model as the tool result.

- `ctx.tool_call` — the `ToolCall` being proposed.
- `ctx.runtime` — current runtime flags (plan_mode, yolo_mode).
- `ctx.tool_result` — `None` (tool hasn't run yet).

Typical uses: mode gates (refuse destructive tools in plan mode),
path-scope checks, yolo-mode auto-approvals, audit logging that can
veto. Always set `fire_and_forget=False`.

## PostToolUse

Fires after a tool returns. Fire-and-forget — return values are
ignored. Exceptions are caught and logged.

- `ctx.tool_call` — the call that ran.
- `ctx.tool_result` — the `ToolResult` from `execute`.
- `ctx.runtime` — the runtime flags that were active.

Typical uses: log tool calls + results to an audit file, flag unusual
results for later review, record latency. NEVER mutate shared state —
multiple PostToolUse hooks run concurrently.

## Stop

Fires when the model's step returns without requesting another tool
call — the natural turn end. A handler returning `HookDecision(decision=
"block", reason=<msg>)` forces another iteration; the reason becomes
the next user-visible system prompt.

- `ctx.tool_call`, `ctx.tool_result` — both `None`.
- `ctx.message` — the final assistant message for the turn.

Typical uses: require a "did you update the TODOs?" reminder before the
agent can end, enforce a "must finish tests" gate for coding tasks.
Blocking.

## SessionStart

Fires once per session right after the `SessionDB` row is created.
Fire-and-forget.

- `ctx.session_id` — populated.
- Everything else `None`.

Typical uses: prime per-session directories (`~/.opencomputer/harness/
<session_id>/`), record session start time, emit telemetry.

## SessionEnd

Fires when the loop exits cleanly (natural `END_TURN` or explicit
user exit). Not fired on crashes.

- `ctx.session_id` — populated.

Typical uses: flush caches, summarize the session for later retrieval,
clean up temp files.

## UserPromptSubmit

Fires when the user submits a message before the agent processes it. A
handler returning `HookDecision(decision="block", modified_message=X)`
replaces the user's text with `X` before the loop sees it.

- `ctx.message` — the incoming user message.

Typical uses: expand shorthand commands, inject project context, apply
safety transforms, scan for demand-driven plugin activation triggers
(sub-project E.E7 uses this).

## PreCompact

Fires immediately before the `CompactionEngine` summarizes old turns
when the context fills. Fire-and-forget.

- `ctx.session_id` — populated.

Typical uses: save a full transcript to disk before compaction lossy-
summarizes it, notify the user that compaction is happening.

## SubagentStop

Fires when a delegate-spawned subagent finishes its own loop. Emitted
from `opencomputer/tools/delegate.py::DelegateTool.execute` after the
child `run_conversation` returns. Fire-and-forget.

- `ctx.session_id` — the PARENT session id (the subagent has its own
  but the hook fires in the parent's loop).
- `ctx.runtime` — the runtime that was passed to the subagent.

Typical uses: roll up subagent results into the parent session log,
track delegation usage, emit token-count telemetry.

## Notification

Fires once per dispatched notification when `PushNotification` sends a
message to an external channel (Telegram, Discord, etc.). One fire per
channel adapter that accepts the notification. Fire-and-forget.

- `ctx.message` — the notification payload.

Typical uses: dedupe notifications across channels, record deliveries,
apply channel-specific rewrites.

## HookContext quick reference

```python
@dataclass(frozen=True, slots=True)
class HookContext:
    event: HookEvent
    session_id: str
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    message: Message | None = None
    runtime: RuntimeContext | None = None   # None on pre-6a hooks
```

The nullable fields mean every handler should check before dereferencing.
A `PreToolUse` handler that assumes `ctx.tool_call` is always populated
is correct today; an audit handler that wants to run on every event
must defensively handle the missing cases.
