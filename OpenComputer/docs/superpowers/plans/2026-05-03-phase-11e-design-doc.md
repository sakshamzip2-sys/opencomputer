# Phase 11e — AskUserQuestion in Async Channels — Design Doc Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a single, comprehensive design document that specifies how `AskUserQuestion` will work in async channels (Telegram, Discord, WebSocket wire) — the deferred Phase 11e work — covering schema, agent-loop suspend/resume, channel adapter integration, handler protocol extension, edge cases, and a phased rollout plan.

**Architecture:** Doc-only deliverable (no production code). Lives in `docs/superpowers/specs/`. Lands as a draft PR for human review before any implementation work begins.

**Tech Stack:** Markdown only.

---

## File Structure

- Create: `OpenComputer/docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md`
  - Six sections (A–F) per the spec, each self-contained.
- Modify: nothing else (this is a design-only PR).
- Test: nothing — markdown doc.

---

### Task 1: Read all referenced source files to ground the design

**Files (read-only):**
- `OpenComputer/opencomputer/tools/ask_user_question.py` — current tool, gateway-mode error path
- `OpenComputer/opencomputer/cli_ui/ask_user_question_handler.py` — CLI Rich-Prompt handler (PR #383)
- `OpenComputer/plugin_sdk/interaction.py` — `InteractionRequest` / `InteractionResponse` / `ASK_USER_QUESTION_HANDLER` ContextVar
- `OpenComputer/opencomputer/agent/state.py` — SessionDB schema + MIGRATIONS dict
- `OpenComputer/opencomputer/gateway/dispatch.py` — `Dispatch.handle_message` flow
- `OpenComputer/opencomputer/agent/loop.py` — `AgentLoop.run_conversation` + `_dispatch_tool_calls`

- [ ] **Step 1: Read each file above** with the Read tool. Note the exact line numbers / class names that the design doc will reference.

- [ ] **Step 2: Confirm `SCHEMA_VERSION` and migration mechanism.**
  Verify the migration system uses an integer `SCHEMA_VERSION` + a `MIGRATIONS: dict[tuple[int, int], str]` dict mapping `(from_version, to_version)` to either DDL text or a callable. The design doc cites these by name.

  Run: `grep -n "SCHEMA_VERSION\|^MIGRATIONS" OpenComputer/opencomputer/agent/state.py | head -10`
  Expected: confirms `SCHEMA_VERSION` integer + `MIGRATIONS` dict exist.

### Task 2: Write the design-doc skeleton with all six section headers + frontmatter

**Files:**
- Create: `OpenComputer/docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md`

- [ ] **Step 1: Write the doc skeleton.** This locks in the structure before fleshing out individual sections.

````markdown
# Phase 11e — AskUserQuestion in Async Channels (Design Doc)

> **Status:** Design — no implementation. Land this PR as draft; only break ground after explicit user buy-in on the phased plan in §F.
> **Author:** Saksham (via Claude Code)
> **Date:** 2026-05-03
> **Supersedes:** N/A (greenfield design for the deferred Phase 11e item)
> **Implements:** the design described in the docstring of `OpenComputer/opencomputer/tools/ask_user_question.py` ("Phase 11e adds proper pending-tool support").

## Problem statement

`AskUserQuestion` blocks on synchronous user input. In CLI mode that maps to `console.input("> ")`. In async-channel mode (Telegram, Discord, WebSocket wire), there is no direct mechanism to block on the user's reply — the channel is async and message-driven. Today the tool returns an explicit error in those modes pointing the agent at PushNotification + the user's next inbound message.

This design specifies the mechanism that will let `AskUserQuestion` work natively in async channels: a **pending-tool-call state** persisted in SessionDB, an agent-loop **suspend/resume** flow, channel-adapter routing for the user's resolving message, and a **handler protocol** that fits the existing `ASK_USER_QUESTION_HANDLER` ContextVar contract.

## Constraints (load-bearing)

1. **No change to `plugin_sdk/`** beyond what's already public. The handler contract stays `Awaitable[InteractionResponse]` so existing tools / surfaces don't need modification.
2. **Backwards-compatible schema** — additive only. Existing rows untouched.
3. **Resumable across process restarts.** A pending call must survive `oc gateway` restart.
4. **Tenant safety.** Only the user_id who triggered the original ask can resolve it.
5. **Bounded.** Pending calls expire (default: 6h) so a stalled session can't accumulate state forever.

---

## A. Schema changes

[Filled in Task 3]

## B. Agent loop suspend/resume

[Filled in Task 4]

## C. Channel adapter integration

[Filled in Task 5]

## D. Handler protocol extension

[Filled in Task 6]

## E. Edge cases the design must cover

[Filled in Task 7]

## F. Migration plan / rollout

[Filled in Task 8]
````

- [ ] **Step 2: Verify the file exists + structure looks right.**

  Run: `ls -la OpenComputer/docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md`
  Expected: file exists; ~1.5 KB.

- [ ] **Step 3: Commit.**

```bash
cd /Users/saksham/.config/superpowers/worktrees/claude/phase-11e-design/OpenComputer
git add docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md
git commit -m "design(phase-11e): skeleton + problem statement"
```

### Task 3: Section A — Schema changes

**Files:**
- Modify: `OpenComputer/docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md` (replace `[Filled in Task 3]` under §A)

- [ ] **Step 1: Write Section A** — append under the `## A. Schema changes` heading:

````markdown
### A.1 New table: `pending_tool_calls`

```sql
CREATE TABLE IF NOT EXISTS pending_tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    -- The platform / channel that issued this ask. Lets the dispatcher
    -- route an inbound message to the right pending call without
    -- consulting the session row.
    channel         TEXT NOT NULL,        -- 'telegram' | 'discord' | 'wire' | 'cli'
    -- The chat-id the channel adapter uses to talk to this user. Pair
    -- (channel, chat_id) is what an inbound message arrives with.
    chat_id         TEXT NOT NULL,
    -- The original user_id (Telegram user id, Discord user id, etc.).
    -- Tenant-safety check at resolve time: the resolving message's
    -- user_id must equal this.
    user_id         TEXT,
    -- The Anthropic / OpenAI tool_call_id this pending call corresponds
    -- to. Echoed back into the eventual ToolResult so the LLM correlates.
    tool_call_id    TEXT NOT NULL,
    tool_name       TEXT NOT NULL,        -- always 'AskUserQuestion' in v1
    -- JSON-serialised InteractionRequest (question, options, presentation).
    -- Stored verbatim so the handler can re-render the prompt if the
    -- adapter restarts mid-pending.
    request_payload TEXT NOT NULL,        -- JSON
    created_at      REAL NOT NULL,        -- epoch seconds
    expires_at      REAL NOT NULL,        -- epoch seconds; default created_at + 6h
    resolved_at     REAL,                 -- NULL = still pending
    -- The user's reply text (or 'TIMEOUT' / 'CANCELLED' for non-user
    -- resolutions). The handler maps this back to InteractionResponse.
    resolved_value  TEXT,
    -- 'pending' | 'resolved' | 'expired' | 'cancelled'.
    -- Redundant with resolved_at + expires_at but lets the index lookup
    -- be a single equality test.
    status          TEXT NOT NULL DEFAULT 'pending',
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

-- Active-pending lookup: "is there an outstanding ask for this chat?"
-- Hits exactly one row when present, none when not.
CREATE INDEX IF NOT EXISTS idx_pending_active
    ON pending_tool_calls(channel, chat_id, status)
    WHERE status = 'pending';

-- Expiry sweep: scan rows whose expires_at has passed AND status is
-- still 'pending'.
CREATE INDEX IF NOT EXISTS idx_pending_expiry
    ON pending_tool_calls(expires_at, status)
    WHERE status = 'pending';

-- Per-session in-flight count (parallel-tool-call subagents may have
-- multiple).
CREATE INDEX IF NOT EXISTS idx_pending_session
    ON pending_tool_calls(session_id, status);
```

### A.2 Migration sketch

Append to `MIGRATIONS` in `opencomputer/agent/state.py`:

```python
# Phase 11e: pending_tool_calls — async-channel AskUserQuestion state.
(SCHEMA_VERSION, SCHEMA_VERSION + 1): _MIGRATION_PHASE_11E_DDL,
```

Where `_MIGRATION_PHASE_11E_DDL` is the DDL above. Bump `SCHEMA_VERSION` by 1 in the same change. Existing rows in `sessions` / `messages` are untouched.

### A.3 SessionDB API surface

Add helpers to `opencomputer/agent/state.py::SessionDB`:

| Method | Purpose |
|---|---|
| `create_pending_tool_call(session_id, channel, chat_id, user_id, tool_call_id, tool_name, request_payload, ttl_seconds=21600) -> int` | Insert a row, return its `id`. Caller passes `tool_call_id` from the LLM response. |
| `find_active_pending(channel, chat_id) -> PendingToolCall | None` | Single-row equality lookup on the `idx_pending_active` index. |
| `resolve_pending(id, resolved_value) -> bool` | Set `resolved_at = now`, `status = 'resolved'`, `resolved_value = ...`. Returns True iff exactly one row updated AND its status was `'pending'` (atomic state transition). |
| `cancel_pending(id, reason) -> bool` | Same shape but `status = 'cancelled'`, `resolved_value = reason`. |
| `expire_pending_calls(now) -> int` | Sweep rows with `expires_at < now AND status = 'pending'`, mark `'expired'`, return count. Called by the gateway's existing periodic `system_tick` cron. |
| `count_pending_for_session(session_id) -> int` | For the parallel-tool-call edge case (§E.3). |

A frozen `@dataclass` `PendingToolCall` holds the row shape. Lives in `opencomputer/agent/pending.py` (new file), not `state.py`, so `state.py` doesn't grow. Re-export from `state.py.__init__` so callers do `from opencomputer.agent.state import PendingToolCall`.
````

- [ ] **Step 2: Commit.**

```bash
git add docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md
git commit -m "design(phase-11e): §A schema + migration + SessionDB API"
```

### Task 4: Section B — Agent loop suspend/resume

**Files:**
- Modify: `OpenComputer/docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md` (replace `[Filled in Task 4]`)

- [ ] **Step 1: Write Section B** — append:

````markdown
### B.1 Where the loop pauses

`AgentLoop.run_conversation` (in `opencomputer/agent/loop.py`) executes a model turn → dispatches tool calls → loops until the model stops calling tools. Tool dispatch happens in `_dispatch_tool_calls` which awaits each `BaseTool.execute(call)`.

For a synchronous tool (the current contract), `execute` runs to completion and returns a `ToolResult`. For Phase 11e, we introduce a **pending sentinel**: when the async-channel handler receives an `InteractionRequest` and there's no synchronous reply path, it persists the pending row, then raises `PendingToolCallSuspension(pending_id=...)`. The agent loop catches this exception in `_dispatch_tool_calls`, finalises the conversation state to disk (turn marked `suspended`), and **returns from `run_conversation` with `outcome=ConversationOutcome.SUSPENDED`** instead of the usual `COMPLETED`.

```python
@dataclass(frozen=True, slots=True)
class PendingToolCallSuspension(Exception):
    pending_id: int                 # PK in pending_tool_calls
    tool_call_id: str               # echoed back to the LLM on resume
```

`ConversationOutcome` (in `opencomputer/agent/loop.py`) gains a third variant:

```python
class ConversationOutcome(Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"   # existing
    SUSPENDED = "suspended"   # NEW — at least one pending tool call outstanding
```

The gateway dispatcher (next section) treats `SUSPENDED` as a clean exit and **does not** start another turn. The session row's `ended_at` stays NULL because the conversation isn't actually over.

### B.2 How the pending state is persisted

The `AsyncChannelAskHandler` (Section D) is responsible for the `INSERT` into `pending_tool_calls`. The agent loop does NOT touch the table directly — it only sees the suspension exception. This keeps the agent layer ignorant of the channel-specific schema and matches the existing pattern of "tools own their I/O".

The handler's persistence uses the same SessionDB connection the loop is using (passed via the gateway's per-session `RuntimeContext.custom["_session_db"]`). Same transaction → same WAL — no new lock surface.

### B.3 Resume

When the gateway dispatcher sees an inbound message and `find_active_pending(channel, chat_id)` returns a row:

1. Validate sender (Section E.1) — reject if mismatched user_id.
2. Resolve the row: `db.resolve_pending(pending_id, message.text)`.
3. Construct a synthetic `ToolResult`:

```python
ToolResult(
    tool_call_id=pending_row.tool_call_id,
    content=f"User answered: {message.text}",  # or option-index format
    is_error=False,
)
```

4. Re-enter `AgentLoop.run_conversation` with a special `resume_with: list[ToolResult]` kwarg that injects these results before the next provider call instead of treating the inbound message as a new user turn.

```python
async def run_conversation(
    self,
    user_msg: Message | None = None,
    *,
    runtime: RuntimeContext,
    resume_with: list[ToolResult] | None = None,
) -> ConversationResult:
    ...
```

When `resume_with` is non-None, the loop skips the "append user message" step and goes straight to the model call with the pending tool results filled in. The LLM sees a normal continuation: tool results came back, here's the next assistant turn.

### B.4 Restart safety

If `oc gateway` restarts while pending rows exist, the next `system_tick` (existing cron infrastructure in `opencomputer/agent/system_tick.py`) calls `expire_pending_calls(now)` to sweep rows past `expires_at`. Rows still within their TTL just keep waiting — the next inbound message for that chat triggers the resume path naturally.

For a row whose session is no longer addressable (orphaned: gateway lost track of the session_id mapping), the design treats this as **timeout-on-next-tick**: the expiry sweep marks them `'expired'` and writes a synthetic ToolResult with `is_error=True, content="User did not reply within timeout (gateway restarted)"`. The next `run_conversation` resume picks it up.

### B.5 Session lifecycle during suspension

A `SUSPENDED` outcome does **NOT** close the session:

- `sessions.ended_at` stays NULL (the conversation isn't done; it's paused).
- `sessions.input_tokens` / `output_tokens` accumulators are flushed for the partial turn that produced the `AskUserQuestion` call (we don't lose accounting for tokens that were paid for).
- The `messages` row for the assistant turn that issued the tool call is written normally — the model's reasoning and tool_use block are persisted. The corresponding tool_result row is NOT yet written; it lands when the user resolves.
- On resume via `resume_with`, the loop appends the synthetic tool_result message to `messages` first, THEN calls the provider — preserving the strict tool_use/tool_result pairing that compaction and Anthropic's API both require.
````

- [ ] **Step 2: Commit.**

```bash
git add docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md
git commit -m "design(phase-11e): §B agent-loop suspend/resume contract"
```

### Task 5: Section C — Channel adapter integration

**Files:**
- Modify: `OpenComputer/docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md` (replace `[Filled in Task 5]`)

- [ ] **Step 1: Write Section C** — append:

````markdown
### C.1 Adapter responsibility

Channel adapters (extensions/telegram/, extensions/discord/) already implement `BaseChannelAdapter.handle_inbound(event: MessageEvent) -> ProcessingOutcome`. The Phase 11e change is purely additive: BEFORE delegating to `Dispatch.handle_message` (which would start a new turn), the adapter checks for an outstanding pending call and routes accordingly.

```python
# In extensions/telegram/adapter.py:handle_inbound (and Discord equivalent)
async def handle_inbound(self, event: MessageEvent) -> ProcessingOutcome:
    db = self._get_session_db()  # already wired
    pending = db.find_active_pending(channel=event.platform, chat_id=event.chat_id)
    if pending is not None:
        # Tenant safety — see E.1
        if pending.user_id and pending.user_id != event.user_id:
            await self._send(event.chat_id, "Only the original asker can answer this.")
            return ProcessingOutcome.HANDLED
        # Hand off to Dispatch.resume_pending — does the resolve + agent-loop resume.
        return await self._dispatch.resume_pending(pending.id, event.text)
    # No pending call → existing path
    return await self._dispatch.handle_message(event)
```

This keeps the adapter thin: one extra DB call per inbound. The `find_active_pending` index makes it O(1).

### C.2 Outbound prompt rendering

When the agent loop suspends, the handler that performed the INSERT also calls `adapter.send_question(chat_id, request)`. The adapter chooses presentation:

- **Telegram:** if `request.options` non-empty → ReplyKeyboardMarkup with one button per option + a "(other — type your answer)" hint. If empty → plain message ending in `?`.
- **Discord:** if `request.options` non-empty → embed with numbered options + "Reply with the number or your text" footer. If empty → plain message.
- **Wire (WebSocket):** structured JSON message `{"type": "ask_user_question", "request": {...}}`. The TUI / IDE client renders.

A new `BaseChannelAdapter.send_question(chat_id, request) -> None` method becomes part of the contract. Adapters that don't override it inherit a default that just sends `request.question` as plain text (graceful degradation; fine for Slack-without-buttons and similar).

### C.3 Resolve-message format

The user's reply text reaches the adapter as a normal `MessageEvent`. The adapter routes it via the §C.1 check — no special command syntax needed.

If the user typed a number AND `request.options` has entries → numeric option resolution (matches the existing CLI handler logic in `_resolve_option`). Otherwise free-form text passes through as `InteractionResponse(text=raw, option_index=None)`.

### C.4 Stable session binding

The gateway already maintains `(adapter, chat_id) → session_id` in `Dispatch._session_bindings` (Round 2a P-5, line 383 in dispatch.py). The pending-tool-call row's `session_id` MUST equal whatever that binding resolves to at insert time. If a user starts a new session while a pending call is outstanding → the pending row's session_id is stale; the next inbound's binding lookup may now point elsewhere. The expiry sweep cleans this up; the design accepts ~6h of dead state as the tradeoff.

### C.5 Subagent inheritance

`ASK_USER_QUESTION_HANDLER` is a `ContextVar`, which inherits across `asyncio.Task` boundaries (Python's `Context.copy()` semantics). A subagent spawned via `DelegateTool` runs in a copied context — so it sees the **parent** session's installed handler. Concretely: a Telegram subagent's `AskUserQuestion` will route to the parent's Telegram chat, not to a separate channel.

This is **intentional and desirable** — a subagent's question must reach a real user, and the parent's already-bound chat is the right place. The pending row's `session_id` is the parent's session, not a synthetic subagent id, which keeps the expiry sweep simple. Document this in the implementation's docstring.
````

- [ ] **Step 2: Commit.**

```bash
git add docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md
git commit -m "design(phase-11e): §C channel-adapter integration"
```

### Task 6: Section D — Handler protocol extension

**Files:**
- Modify: `OpenComputer/docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md` (replace `[Filled in Task 6]`)

- [ ] **Step 1: Write Section D** — append:

````markdown
### D.1 The contract stays the same

`plugin_sdk.interaction.AskUserQuestionHandler` is already `Protocol[(InteractionRequest) -> Awaitable[InteractionResponse]]`. The async-channel handler is just a different implementation of this Protocol — no SDK change needed.

### D.2 The new handler

```python
# opencomputer/gateway/async_channel_ask_handler.py (new file)

@dataclass(frozen=True, slots=True)
class AsyncChannelAskHandler:
    """Handler that suspends the agent loop on a pending tool-call row
    instead of blocking on stdin.

    Installed by the gateway worker per-session at session start (analogous
    to cli.py's install_rich_handler). Closes over the SessionDB + the
    bound channel adapter so it can both persist + render the question.
    """

    db: SessionDB
    adapter: BaseChannelAdapter
    session_id: str
    channel: str            # adapter platform name
    chat_id: str
    user_id: str | None
    tool_call_id_resolver: Callable[[], str]   # returns the tool_call_id of the AskUserQuestion call currently dispatching

    async def __call__(self, req: InteractionRequest) -> InteractionResponse:
        pending_id = self.db.create_pending_tool_call(
            session_id=self.session_id,
            channel=self.channel,
            chat_id=self.chat_id,
            user_id=self.user_id,
            tool_call_id=self.tool_call_id_resolver(),
            tool_name="AskUserQuestion",
            request_payload=json.dumps(asdict(req)),
        )
        await self.adapter.send_question(self.chat_id, req)
        # Suspend the loop. The exception propagates up through
        # _dispatch_tool_calls; the loop catches it, marks the
        # conversation SUSPENDED, and returns.
        raise PendingToolCallSuspension(pending_id=pending_id, tool_call_id=self.tool_call_id_resolver())
```

### D.3 Where `tool_call_id_resolver` comes from

`BaseTool.execute(call)` already receives the `ToolCall` object whose `id` is what we need. The handler can't see the call directly (it just gets `InteractionRequest`), so the dispatch layer passes the id via a `ContextVar`:

```python
# plugin_sdk/interaction.py — add alongside ASK_USER_QUESTION_HANDLER
CURRENT_TOOL_CALL_ID: ContextVar[str | None] = ContextVar("CURRENT_TOOL_CALL_ID", default=None)
```

Set in `_dispatch_tool_calls` immediately before each `await tool.execute(call)`, reset after. The handler's `tool_call_id_resolver` is just `lambda: CURRENT_TOOL_CALL_ID.get()`.

**SDK boundary requirements** (per `plugin_sdk/CLAUDE.md`):
- Add `CURRENT_TOOL_CALL_ID` to `plugin_sdk/__init__.py:__all__`.
- Add `CURRENT_TOOL_CALL_ID` to the from-import block in `plugin_sdk/__init__.py`.
- Plugins consume via `from plugin_sdk import CURRENT_TOOL_CALL_ID`, never via the submodule.
- Adding a new public name is a minor-version-compat change (allowed); do NOT remove or rename anything in `__all__`.

### D.4 Install path

The gateway worker, after binding `(adapter, chat_id) → session_id`, calls:

```python
ASK_USER_QUESTION_HANDLER.set(
    AsyncChannelAskHandler(
        db=session_db,
        adapter=adapter,
        session_id=session_id,
        channel=adapter.platform_name,
        chat_id=event.chat_id,
        user_id=event.user_id,
        tool_call_id_resolver=lambda: CURRENT_TOOL_CALL_ID.get(),
    )
)
```

Mirrors `cli.py:install_rich_handler` exactly. The CLI install is unchanged.
````

- [ ] **Step 2: Commit.**

```bash
git add docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md
git commit -m "design(phase-11e): §D handler protocol extension"
```

### Task 7: Section E — Edge cases

**Files:**
- Modify: `OpenComputer/docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md` (replace `[Filled in Task 7]`)

- [ ] **Step 1: Write Section E** — append:

````markdown
### E.1 User exits the chat before answering

Detection: there's no event for "user left a Telegram chat" — Telegram's API doesn't notify the bot. So this collapses into the timeout case (§E.2). After `expires_at`, the expiry sweep marks the row `'expired'` and synthesises:

```python
ToolResult(
    tool_call_id=pending.tool_call_id,
    content="Error: user did not reply within 6 hours (timeout)",
    is_error=True,
)
```

The agent loop resumes with this result on the next `system_tick`. The model sees a tool error and can decide what to do (give up, ask again later, take a default action).

### E.2 Timeout

Default TTL: **6 hours**, configurable per-call via an optional `timeout_seconds` field on `InteractionRequest` (additive — defaults to None which means "use system default"). Rationale: short enough to bound state growth, long enough for typical "leave it overnight" Telegram use.

The gateway's `system_tick` cron (already wired) calls `expire_pending_calls(now)` every minute. Sweep is a single indexed query → cheap.

### E.3 User asks a NEW question while a pending call is outstanding

Concrete scenario: bot asked "deploy to prod?" — instead of answering, user types "what's the weather?".

Two valid behaviours; the design picks **(a)** because it preserves agent state cleanly:

(a) **Treat the new message as the answer.** The pending call resolves with `text="what's the weather?"` and `option_index=None`. The model then sees that as the user's reply and decides what to do (most likely it'll either retry the ask or treat the off-topic message as an implicit "no" / "cancel"). The user can always re-ask afterwards.

(b) Reject the new message: "You have an outstanding question — please answer it first or type /cancel". This shifts cognitive load to the user and creates a wedge state if the user has truly moved on.

The design picks (a). A short hint message ("Resolving your previous ask with this reply…") is sent to the user before the agent loop resumes, so the behaviour is observable not magical.

### E.4 Multiple pending calls outstanding (parallel tool calls)

The agent loop supports parallel tool dispatch (`asyncio.gather` over `BaseTool.execute` calls in `_dispatch_tool_calls`). If multiple `AskUserQuestion` calls fire in parallel — possible if a subagent fans out or the model emits two asks in one assistant turn — each would otherwise get its own `pending_tool_calls` row, and the user would see TWO Telegram messages back-to-back with no clear ordering. That UX is unacceptable.

**v1 mitigation: serialize per session.** Inside `AsyncChannelAskHandler.__call__`, before the INSERT:

```python
existing = self.db.count_pending_for_session(self.session_id)
if existing > 0:
    return InteractionResponse(
        text="",
        option_index=None,
    )._replace(  # frozen dataclass — returned as-is, the tool layer
                  # converts to ToolResult(is_error=True, content=...)
        text="Error: another AskUserQuestion is already outstanding for this session. "
             "Resolve it first."
    )
```

The handler returns an error response → the tool produces an error ToolResult → the model sees the error and either retries later or proceeds without asking. The single-active-pending invariant simplifies user UX (one question on screen at a time) and the resolution dispatch (no FIFO ambiguity).

For the rare legitimate case (subagent A and subagent B both genuinely need user input concurrently), the recommended pattern is for the parent agent to ask one consolidated question. The design explicitly does NOT support concurrent independent asks in v1.

When the next inbound message arrives, `find_active_pending` returns the single outstanding row (no FIFO needed because there's only one). On resolution, the OTHER subagent's NEXT ask succeeds.

### E.5 Session compaction while pending

`CompactionEngine` summarises old turns to keep the model's context window bounded. Pending tool calls **must not** be compacted away — the LLM needs to see the original tool_call_id when the resume injects the result.

Constraint added to `CompactionEngine._safe_split_index`: extend its existing "atomic tool_use/tool_result pair" guard to also exclude any tool_use whose `tool_call_id` appears in the active `pending_tool_calls` table. Call site: query `db.list_active_pending_ids(session_id)` once per compaction run, exclude those ids from the split point.

### E.6 Concurrent resolution race

Two inbound messages arrive within the same DB tick (rare but possible on fast Discord channels). The `resolve_pending(id, value)` method is atomic on `WHERE id = ? AND status = 'pending'`; the second UPDATE returns 0 affected rows and the adapter sends "this question was already answered" to the user.

### E.7 Session deletion while pending

If `oc session delete` (or admin DB ops) removes a session row while one of its `pending_tool_calls` is open: the `ON DELETE CASCADE` on the FK drops the pending row immediately. The next inbound message from that user's chat finds nothing in `find_active_pending` and routes as a normal new turn. No special handling needed; this is the desired behavior (session is gone, the question is moot).

If you DO want a paper trail, a follow-on enhancement could log the cascade-deleted pending rows to `audit_log` (Phase F1 consent infrastructure); v1 doesn't bother.

### E.8 Cleanup of resolved/expired rows

Without cleanup, the table grows unbounded. v1 strategy: piggyback on the existing `system_tick` cron — each tick, after `expire_pending_calls`, also run:

```sql
DELETE FROM pending_tool_calls
WHERE status IN ('resolved', 'expired', 'cancelled')
  AND resolved_at < (now - 7 * 86400);   -- 7 days
```

7 days is enough for "what did I answer?" debugging via the `oc insights` command (which can join messages → pending_tool_calls.resolved_value to reconstruct the user's reply that drove a particular tool result). Older history is in `messages` already — the synthetic ToolResult is stored there permanently.
````

- [ ] **Step 2: Commit.**

```bash
git add docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md
git commit -m "design(phase-11e): §E edge cases (timeout, new ask, parallel, compaction, race)"
```

### Task 8: Section F — Migration / rollout plan

**Files:**
- Modify: `OpenComputer/docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md` (replace `[Filled in Task 8]`)

- [ ] **Step 1: Write Section F** — append:

````markdown
### F.1 Phased rollout

Each phase is shippable independently. **Effort estimates assume one engineer, IDE-paired with Claude Code, no new requirement scope creep.**

#### Phase 11e.1 — Schema + handler API + agent-loop suspend (~3-4 days)

- Add `pending_tool_calls` table + migration.
- Add `PendingToolCall` dataclass (in new `opencomputer/agent/pending.py`) + SessionDB methods (§A.3) + cleanup-on-system-tick (§E.8).
- Add `PendingToolCallSuspension` exception + `ConversationOutcome.SUSPENDED`.
- Add `CURRENT_TOOL_CALL_ID` ContextVar to `plugin_sdk/interaction.py` + export from `plugin_sdk/__init__.py:__all__` (§D.3).
- Wire `_dispatch_tool_calls` to set/reset `CURRENT_TOOL_CALL_ID` around each `tool.execute(call)` (token-reset pattern, must use try/finally).
- Add `AsyncChannelAskHandler` (§D.2) including the per-session serialization check (§E.4).
- `run_conversation(resume_with=...)` resume path (§B.3) including session-lifecycle handling (§B.5).
- Tests: unit tests for SessionDB methods (~6), suspension exception flow (~3), resume injection (~3), per-session serialization (~2), session-cascade-on-delete (~1).
- **Ship behind no flag** — handler is only invoked when a gateway worker explicitly installs it; CLI continues using `RichHandler`.

Estimate revised upward from initial 2 days because: (a) the suspend exception flow plus `resume_with` injection both touch `_dispatch_tool_calls` which has nontrivial parallel-dispatch + hook-firing logic; (b) the SDK export needs care to keep the boundary test green; (c) the CompactionEngine integration (§E.5) is the kind of cross-cutting change that always uncovers latent assumptions.

#### Phase 11e.2 — Telegram only (~1.5 days)

- `BaseChannelAdapter.send_question` default + Telegram override (ReplyKeyboardMarkup).
- Telegram `handle_inbound` shim that checks `find_active_pending` first (§C.1).
- Gateway worker installs `AsyncChannelAskHandler` per-session at chat-bind time.
- Expiry sweep wired into existing `system_tick`.
- Tests: integration test with a fake Telegram adapter that simulates the round trip.
- **Ship gated on `OPENCOMPUTER_PHASE_11E_TELEGRAM=1` env var** for the first week of use to limit blast radius.

#### Phase 11e.3 — Discord (~1 day)

- Discord override for `send_question` (embed + numbered footer).
- Same `handle_inbound` shim pattern.
- **Ship gated on `OPENCOMPUTER_PHASE_11E_DISCORD=1`** for the first week.

#### Phase 11e.4 — WebSocket wire server (~1 day)

- Add `ask_user_question` message type to the wire protocol (`opencomputer/gateway/protocol.py`).
- TUI / IDE client renders + sends a `pending_resolution` reply.
- Same handler install path; wire-side adapter implements `send_question` as a structured JSON push.
- **Ship gated on protocol-version negotiation** — the wire server advertises `supports_ask_user_question: true`; older clients still get the v1 error path.

#### Phase 11e.5 — Drop the v1 error path (~30min, one quarter after .2)

- Remove the `if not self._cli_mode: return error` branch in `tools/ask_user_question.py`.
- The handler check (`ASK_USER_QUESTION_HANDLER.get()`) becomes the only path. If no handler is installed AND we're in async mode, the handler is None → tool errors with a clearer message ("no AskUserQuestion handler installed for this surface — channel adapter not Phase-11e ready").

### F.2 Total estimated effort

**7-9 engineer-days** across 11e.1–.5 (revised from initial 6-day estimate after audit — suspension flow + SDK boundary + serialization edge case all needed more work than first sketched). Spread across at least 2 calendar weeks so .2 / .3 each get a soak week before .4 lands.

### F.3 Open questions for the user before kickoff

1. **TTL default** — 6h reasonable, or do we want a shorter default (e.g. 1h) for safety, with explicit opt-in for long-running asks?
2. **Tenant safety strictness** — reject mismatched user_id silently, or send an explanatory message? Current proposal: explanatory message ("Only the original asker can answer").
3. **Edge case E.3 default** — do you agree treating the new message as the answer is the right default, or would you prefer reject + hint?
4. **Parallel-asks behavior (§E.4)** — current proposal is "second concurrent ask returns an error to the model so it can decide". Alternative: queue the second ask and ask it after the first resolves. Queue is more user-friendly but harder to reason about across long pauses. Confirm which side to land on.
5. **Should Phase 11e.5 (drop v1 error path) be tied to a major version bump**, or land on minor as a deprecation?

These are the only 5 calls that need your sign-off before kicking off implementation.
````

- [ ] **Step 2: Commit.**

```bash
git add docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md
git commit -m "design(phase-11e): §F phased rollout (.1–.5) + effort estimate + open questions"
```

### Task 9: Self-review the doc against the cloud-routine spec

- [ ] **Step 1: Re-read the doc end-to-end.** Open `docs/superpowers/specs/2026-05-03-phase-11e-askuserquestion-async.md` and verify every section A–F has substantive content (no `[Filled in Task N]` placeholders left).

- [ ] **Step 2: Verify against the spec checklist.** The cloud-routine prompt required:
  - §A: pending_tool_calls table + migration + indexes ✅
  - §B: where loop pauses, persistence, inbound routing, restart safety ✅
  - §C: adapter routing, auth, message shape ✅
  - §D: handler protocol install path ✅
  - §E: 4 edge cases (exit, new ask, parallel, compaction) — plus we added .6 race ✅
  - §F: phased rollout per phase ✅

  If any section is missing content, append it before continuing.

- [ ] **Step 3: Confirm git is clean.**

  Run: `git status -s`
  Expected: empty output (all changes committed).

### Task 10: Push branch + open draft PR

- [ ] **Step 1: Push the branch.**

```bash
cd /Users/saksham/.config/superpowers/worktrees/claude/phase-11e-design/OpenComputer
git push -u origin design/phase-11e-async-askuserquestion
```

- [ ] **Step 2: Open draft PR.**

```bash
gh pr create --repo sakshamzip2-sys/opencomputer \
  --head sakshamzip2-sys:design/phase-11e-async-askuserquestion \
  --base main \
  --draft \
  --title "design(phase-11e): AskUserQuestion in async channels — schema + suspend/resume + rollout plan" \
  --body "$(cat <<'EOF'
## Summary

Design-only PR (no production code). Specifies how AskUserQuestion will work natively in Telegram / Discord / WebSocket channels (the deferred Phase 11e item).

Six sections per the original scope:
- **A.** Schema: \`pending_tool_calls\` table + migration + indexes + SessionDB API
- **B.** Agent-loop suspend/resume: \`PendingToolCallSuspension\` + \`ConversationOutcome.SUSPENDED\` + \`run_conversation(resume_with=...)\`
- **C.** Channel-adapter integration: \`find_active_pending\` shim before \`handle_message\`, \`send_question\` adapter method, session binding
- **D.** Handler protocol extension: \`AsyncChannelAskHandler\` + \`CURRENT_TOOL_CALL_ID\` ContextVar
- **E.** Edge cases: timeout, user-asks-new-thing, parallel calls, compaction, concurrent resolution race
- **F.** Phased rollout (5 phases, ~6 engineer-days total) + 4 open questions for sign-off

## Why draft

Wants explicit user buy-in on the 4 open questions in §F.3 before kickoff.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Confirm PR opened.**

  Output of step 2 should be a URL like `https://github.com/sakshamzip2-sys/opencomputer/pull/<N>`. Note the PR number for the final report.
