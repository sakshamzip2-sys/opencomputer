# OpenClaw Tier 1 Port — AMENDMENTS

**Companion to:** `2026-04-28-openclaw-tier1-port-AUDIT.md` (verdict: RED).
**Status:** Plan revised. Original 8-pick scope reduced to 7. Three picks reframed.
**Pre-execution gate:** Phase 0 verification MUST run before any sub-project branch is cut. No exceptions.

This document is the canonical fix-list. The original plan + spec stand; this overrides them where conflicts exist.

---

## Headline changes

| Old | New | Why |
|---|---|---|
| 8 picks | **7 picks** | 1.H dropped — file collision with PR #222 |
| 1.B sub-agent (M, 2d) | **1.B-alt RecallTool-prepend (S, ~1d)** | C2 invalidates premise; engine surgery is L not S; cheaper alternative ships actual value |
| 1.D (S, half day) | **1.D (M, ~1.5d)** | H6 — schema migration needed |
| 1.E (S, 1d) | **1.E "thin config surface" (S, ~1d)** | C3 — reframe around existing per-key quarantine, no new abstraction |
| 1.F SessionsSpawn+Send | **DEFERRED** to a future plan | C8 — spawn requires real infra, not a tool wrapper |
| 1.F SessionsList/History/Status | **KEEP as read-only trio** | These are trivial, real, useful |
| 1.A wrapper | **wrap delta-handler at dispatch, not adapter.send** | C7 — adapters use `edit_message`, not `send`, for streaming |

**Total revised effort:** ~7-8 engineering days (was ~10-11).

---

## Critical defect fixes (one per audit C-defect)

### Fix C1 — outgoing_queue API

**Audit finding.** The plan called `outgoing_queue.put_send(channel, peer, message)`. Real API at `opencomputer/gateway/outgoing_queue.py:142` is `enqueue(*, platform, chat_id, body, attachments=None, metadata=None) -> OutgoingMessage`.

**Fix.** Affects 1.F (SessionsSend) and 1.H (SendMessage). 1.H is dropped (C6). 1.F's SessionsSend is deferred (C8). **Net effect: this defect is sidestepped by the scope cut.**

---

### Fix C2 — HookDecision.modified_message NOT honored for PRE_LLM_CALL

**Audit finding.** `agent/loop.py:1825-1841` fires PreLLMCall as `engine.fire_and_forget(...)` and explicitly ignores returns. Sub-project B's design is fundamentally broken — hook would fire, return is discarded, zero behavior change.

**Fix — pivot to a simpler approach: 1.B-alt "RecallTool-prepend"**

Instead of a sub-agent + hook + decision-cache architecture, ship a much simpler version that delivers ~80% of the value at ~25% of the cost:

- **What it does.** On every eligible turn (DM or group, configurable), AgentLoop unconditionally calls the existing `RecallTool` with the user's most recent message as the query (at top of turn, before `provider.complete()`). The top-N (default 3) hits are prepended to the upcoming `messages` list as a single user-role `<relevant-memories>` block — the same kind of system-reminder framing the rest of OC uses.
- **No new hook event needed.** No engine refactor. No HookDecision splice. The injection happens directly in `AgentLoop.run_conversation` BEFORE the loop's first `provider.complete()`.
- **No new sub-agent.** RecallTool already does the LLM-mediated synthesis (via its existing `search` and `note` actions). We just call it ourselves on every turn instead of waiting for the model to call it.
- **Caching.** Same `(session_id, last_user_msg_hash)` key with TTL — but cache becomes "RecallTool result hash" not "decision". Cache hit reuses the prior block.
- **Disable per session.** `/active-memory pause | resume | status` slash commands set `session_state["active_memory_paused"]` — checked at injection point.
- **Default OFF**, opt-in via `oc plugin enable active-memory` (or rather, since this is a core feature now — opt-in via `~/.opencomputer/<profile>/config.yaml::active_memory.enabled: true`).

**Files (revised):**
- Modify: `opencomputer/agent/loop.py` — add `_maybe_prepend_active_memory()` call right before the first `provider.complete()` of a turn.
- Create: `opencomputer/agent/active_memory.py` — small module: `ActiveMemoryInjector(recall_tool, cache, enabled, top_n)`.
- Create: `tests/agent/test_active_memory.py` — unit tests for injector + cache.
- Modify: `opencomputer/agent/config.py` — add `active_memory: ActiveMemoryConfig` block.
- Modify: `opencomputer/cli_ui/slash_handlers.py` — add `/active-memory pause|resume|status`.
- **NOT a plugin** anymore. This is core, lives in `opencomputer/agent/`. Simpler.

**Effort:** S (~1 engineering day) instead of M (~2 days) or L (~3-4 days for the engine surgery).

**Trade-off honestly stated.** We lose the ability to give the recall sub-agent its own model + prompt-style. RecallTool's existing logic is the recall logic. If the user later wants the prompt-style + model variation, that's a future plan — *which only becomes worthwhile if the simpler version proves the value first.*

If the user prefers the original sub-agent architecture, the 4-day path is documented in C2's audit "Fix" section (engine surgery to make PreLLMCall return-aware). I do not recommend it for this wave.

---

### Fix C3 — CredentialPool API mismatch

**Audit finding.** Real module is `opencomputer/agent/credential_pool.py` (not `plugin_sdk/`). Real surface is `acquire() -> str (key)`, `report_auth_failure(key, reason)`, `with_retry(...)`. Keys are API-key strings, not profile_ids. Existing `report_auth_failure` already quarantines for `rotate_cooldown_seconds=ROTATE_COOLDOWN_SECONDS`. No need for a new abstraction.

**Fix — reframe 1.E as "thin config + monitor surface":**

- **Don't add `cooldown(profile_id, seconds)`.** The pool already has cooldown semantics on key-level via `report_auth_failure`. Don't duplicate.
- **Do add `auth_monitor_loop()`** — opt-in background task that, every `interval_seconds` (default 300s), calls each provider's tiny health-check endpoint with each pool key. On failure, calls existing `report_auth_failure(key, reason="health_check_failed")` so existing quarantine kicks in. On success after prior failure, no-op (existing pool already restores after cooldown expiry).
- **Surface a CLI command `oc auth status`** — list each provider, each key, its quarantine state, time until expiry. Useful for debugging.
- **Document the existing `report_auth_failure` cooldown semantics** in `docs/concepts/auth.md` so users know it's there.

**Files (revised):**
- Modify: `opencomputer/doctor.py` — add `auth_monitor_loop` and `auth_monitor_once`.
- Create: `opencomputer/cli_auth.py` — `oc auth status` subapp.
- Modify: `opencomputer/cli.py` — register `oc auth` subapp.
- Create: `tests/test_auth_monitor.py` — unit + integration tests against the existing pool.
- Modify: `extensions/anthropic-provider/provider.py` + `openai-provider/provider.py` — add a `ping()` method (or use models.list as the check).

**Effort:** S (~1 engineering day) — same as before, just with a correctly-located scope.

---

### Fix C4 — ToolCall.input / ToolResult.output shape

**Audit finding.** `plugin_sdk/core.py:62-77` defines `ToolCall(id, name, arguments)` and `ToolResult(tool_call_id, content, is_error)`. Plan used `.input` / `.output`.

**Fix.** Mechanical global rename in 1.F (read trio), 1.G:
- `call.input["..."]` → `call.arguments["..."]`
- `ToolResult(output=str(X), is_error=Y)` → `ToolResult(tool_call_id=call.id, content=str(X), is_error=Y)`
- Test fixtures: `ToolCall(name=..., input=...)` → `ToolCall(id="t-1", name=..., arguments=...)`

**Reference pattern.** `opencomputer/tools/recall.py` is canonical — every new tool follows that shape. Engineer reads recall.py first before writing any tool.

---

### Fix C5 — SessionDB.get_messages / get_session_summary

**Audit finding.** `state.py:811`'s `get_messages(self, session_id)` has no `limit=`. There is no `get_session_summary` — only `get_session(session_id) -> dict | None`.

**Fix in 1.F SessionsHistory and SessionsStatus:**

```python
class SessionsHistory(BaseTool):
    async def run(self, call: ToolCall) -> ToolResult:
        sid = call.arguments["session_id"]
        limit = int(call.arguments.get("limit", 30))
        msgs = self.db.get_messages(sid)  # no limit kwarg
        msgs = msgs[-limit:]              # slice client-side
        return ToolResult(
            tool_call_id=call.id,
            content=str(msgs),
            is_error=False,
        )


class SessionsStatus(BaseTool):
    async def run(self, call: ToolCall) -> ToolResult:
        sid = call.arguments["session_id"]
        info = self.db.get_session(sid)   # not get_session_summary
        if info is None:
            return ToolResult(tool_call_id=call.id, content=f"unknown session {sid}", is_error=True)
        return ToolResult(tool_call_id=call.id, content=str(info), is_error=False)
```

No change to SessionDB. Slice client-side. Use `get_session` for status.

---

### Fix C6 — PR #222 already ships SendMessage

**Audit finding.** `gh pr view 222` shows `opencomputer/tools/send_message.py` already added. Hard collision.

**Fix.** **DROP Sub-project 1.H entirely.** The capability is being shipped by another session.

If on review the implementations differ (e.g., #222 doesn't gate behind F1), that's a follow-up PR against `main` after #222 lands, not this plan. Out of scope here.

---

### Fix C7 — BaseChannelAdapter.send vs edit_message

**Audit finding.** Adapters use `edit_message` for streaming (in-place updates), not `send` (which creates new messages). The plan's `_maybe_chunk_delta` calling `self.send(...)` would produce N separate Telegram messages instead of 1 edited message.

**Fix in 1.A.** Two changes:

1. **Move the wrapper from BaseChannelAdapter to gateway/dispatch.py.** The dispatch layer is where the chunker decision belongs (per spec §6 Option α; the audit recommended this explicitly).

2. **The chunker still calls `send` (one message per block) — and that's intentional.** Multi-paragraph human-paced delivery means a *new* message per paragraph (Telegram, etc.), not one message edited many times. This matches OpenClaw's documented behavior. The `edit_message` path is for "growing the same message live" — which is what we want to *replace*, not preserve.

3. **Phase 0.2 must verify** with concrete grep of dispatch.py whether the streaming path uses `send` or `edit_message` today, and document explicitly in DECISIONS.md.

So C7 isn't a fix to my code; it's a fix to my mental model. The audit flagged "uses edit_message for streaming" but the chunker's whole purpose is to break the long-edit pattern into discrete sends. Document this explicitly in 1.A's PR description.

**One real change:** drop `_maybe_chunk_delta` from `BaseChannelAdapter`. Move it to `gateway/dispatch.py` as `_dispatch_delta_with_chunker(adapter, chat_id, delta)`. Adapter remains agnostic.

---

### Fix C8 — agent_runner.spawn_async doesn't exist

**Audit finding.** `SessionsSpawn` plan registered `runner=agent_runner` and called `runner.spawn_async(...)` — neither exists.

**Fix.** **DEFER `SessionsSpawn` and `SessionsSend` to a future plan.** They require real cross-process spawn infrastructure (or a SessionDB-mediated "first message bookmark" abstraction that's its own M-level sub-project).

**Sub-project 1.F is now: SessionsList + SessionsHistory + SessionsStatus** — read-only trio. All trivially correct against existing `SessionDB.list_sessions`, `get_messages`, `get_session`.

**Effort revised:** 1.F drops from M (~2-3d) to S (~1d).

---

## High-priority defect fixes

### Fix H1 — plugin-local module name collisions

**Audit finding.** Active Memory's plugin imports `from cache import ...` and `from runtime import ...`. OC's loader's `_PLUGIN_LOCAL_NAMES = ("provider","adapter","plugin","handlers","hooks")` doesn't include these names — collisions possible if any other plugin uses sibling `cache.py` or `runtime.py`.

**Fix.** With 1.B-alt (Fix C2), Active Memory is no longer a plugin — it's core in `opencomputer/agent/active_memory.py`. **H1 is sidestepped by the scope cut.**

If we ever revive the sub-agent variant, the rename rule applies: `active_memory_cache.py` and `active_memory_runtime.py`.

---

### Fix H2 — PluginAPI surface fabrications

**Audit finding.** `api.provider`, `api.memory`, `api.slash_commands().add(...)`, `api.config`, `api.list_enabled_channels()` — none verified to exist. `slash_commands` is a plain `dict[str, Any]` (set via `__setitem__`), not a method.

**Fix.** With 1.B-alt as core (not a plugin), these don't apply to active memory anymore. For 1.G ClarifyTool registration, follow the canonical pattern in `cli.py:254` (`registry.register(AskUserQuestionTool())`) — direct registry registration, no PluginAPI.

**Phase 0.7 — NEW.** Before any sub-project, enumerate `PluginAPI.__dict__` and write the actual attribute names + types into `DECISIONS.md`. This is gating for any future plugin work; not gating for this plan since we sidestep it.

---

### Fix H3 — HookSpec priority

**Audit finding.** Active Memory plugin registered `priority=200` (later). With 1.B-alt this is moot — no hook involved.

**Fix.** Sidestepped by scope cut.

---

### Fix H4 — BlockChunker.feed quadratic

**Audit finding.** Each `feed(delta)` call iterates `_extract_one()` which does `buf.find(...)` from `min_chars` over the full buffer. O(N²) on long replies.

**Fix.** Add cursor tracking:

```python
class BlockChunker:
    def __init__(self, ...):
        ...
        self._scan_from: int = 0  # NEW

    def feed(self, delta: str) -> list[Block]:
        self._buf += delta
        out: list[Block] = []
        while True:
            block = self._extract_one()
            if block is None:
                break
            out.append(block)
            self._scan_from = 0  # reset on emit
        return out

    def _find_boundary(self, buf, sep, *, fence_safe):
        start = max(self.min_chars, self._scan_from)
        idx = buf.find(sep, start)
        ...
        # update self._scan_from = max idx tried
```

Add a perf regression test: feed 10k chars → assert total time < 100ms with min_chars=80, max_chars=1500.

---

### Fix H5 — LoopDetector + delegate scoping

**Audit finding.** `_loop_detector.reset()` at session start doesn't account for delegate-spawned subagents sharing the parent's loop_detector instance.

**Fix.** Verify in Phase 0.8 (NEW) whether DelegateTool spawns share the parent AgentLoop instance or get a fresh one. If shared, scope the detector by `(session_id, depth)`:

```python
class LoopDetector:
    def __init__(self, ...):
        self._frames: dict[tuple[str, int], _Frame] = {}

    def push_frame(self, session_id, depth): ...
    def pop_frame(self, session_id, depth): ...
    def record_tool_call(self, session_id, depth, name, args_hash): ...
```

DelegateTool calls `push_frame()` on entry, `pop_frame()` on exit. Each frame has its own sliding windows.

This is a slightly bigger change than the original C plan but still S effort. Bumping 1.C estimate to S+ (~1.5 days).

---

### Fix H6 — replay/in_flight/ts fields don't exist

**Audit finding.** `Message` has no `replay`, `in_flight`, or `ts` fields. SessionDB has no such columns. The sanitizer is a no-op in production.

**Fix — two-step (M effort, was S):**

1. **Schema migration.** Add `replay BOOLEAN DEFAULT 0`, `in_flight BOOLEAN DEFAULT 0`, `ts REAL DEFAULT NULL` to the messages table. Migration script + version bump.
2. **Writers set them.** Whoever writes a buffered assistant turn marks `replay=True` (gateway pre-restart). Outgoing-queue items mark `in_flight=True` until ACK.
3. Sanitizer reads from the new columns + the existing schema.

**Files added:**
- Create: `opencomputer/migrations/0049_add_replay_columns.py`
- Modify: `opencomputer/agent/state.py::SessionDB` — schema + setters.
- Modify: `opencomputer/gateway/server.py` — set in_flight on enqueue, clear on ack.
- Modify: `opencomputer/gateway/dispatch.py` — set replay=True on pre-shutdown buffered text.
- Modify: `opencomputer/gateway/replay_sanitizer.py` — read from `Message` typed access, not dict.get.

**Effort:** M (~1.5 days) instead of S (half day).

---

### Fix H7 — enabled_channels baked at registration

Sidestepped — H7 was specific to Sub-project 1.H, which is dropped.

---

## Phase 0 expansion

The original 6 verification tasks were correct but **not enforced**. The plan's design used assumptions the verification was supposed to prevent. Hard rule:

> **Phase 0 must run, write `DECISIONS.md`, AND any defect found becomes a plan amendment BEFORE the relevant sub-project's branch is cut.**

Adding 2 new Phase 0 tasks per audit findings:

### Task 0.7: Enumerate PluginAPI attributes (was H2 fix)

- [ ] Read `opencomputer/plugins/registry.py::PluginAPI` end-to-end.
- [ ] List every public attribute and method with type signatures.
- [ ] Document in `DECISIONS.md § 0.7`: the canonical pattern for plugin registration of slash commands, hooks, tool registrations.
- [ ] Note: this plan no longer needs PluginAPI for 1.B (now core), but future plans will.

### Task 0.8: DelegateTool scoping (was H5 fix)

- [ ] Read `opencomputer/tools/delegate.py` to confirm whether subagents share parent's `AgentLoop` instance.
- [ ] Document in `DECISIONS.md § 0.8`: required scope key for `LoopDetector` (per-session vs per-(session, depth)).

### Task 0.9 (new): grep API surface for every code block in the plan

Before cutting any sub-project branch, the implementer runs:

```bash
# Verify every method/attribute the plan calls actually exists on main.
for SYMBOL in 'enqueue' 'put_send' 'put_session_send' 'cooldown' 'available_profiles' 'all_profiles' 'spawn_async' 'get_session_summary' 'list_enabled_channels' '\.input\[' 'ToolResult.*output='; do
  echo "--- ${SYMBOL} ---"
  rg "${SYMBOL}" --type py opencomputer/ plugin_sdk/ extensions/ | head -5
done
```

Each line of output gets reconciled against the plan. Mismatches are amendments, not edits-during-execution.

---

## Revised pick list (final)

| # | Pick | Effort | Status | Files |
|---|------|--------|--------|-------|
| 1.A | Block chunker + humanDelay | M (~2d) | REVISED — wrap at dispatch (not adapter), cursor scan (no quadratic) | plugin_sdk/streaming, gateway/dispatch.py |
| 1.B-alt | Active Memory (RecallTool-prepend) | S (~1d) | PIVOTED — core not plugin, no sub-agent, no hook | opencomputer/agent/active_memory.py, agent/loop.py, cli_ui/slash_handlers.py |
| 1.C | Anti-loop detector | S+ (~1.5d) | KEEP — add per-(session,depth) scoping | opencomputer/agent/loop_safety.py, loop.py |
| 1.D | Replay sanitization | M (~1.5d) | REVISED — schema migration needed | gateway/replay_sanitizer.py + migration + state.py + server.py |
| 1.E | Auth cooldown surface | S (~1d) | REVISED — config + monitor over existing pool | doctor.py, cli_auth.py |
| 1.F-read | Sessions read trio (List/History/Status) | S (~1d) | SCOPE-CUT — Spawn/Send deferred | tools/sessions.py + cli.py |
| 1.G | ClarifyTool | S (~half d) | KEEP — fix ToolCall/Result shape | tools/clarify.py + cli.py |
| ~~1.H~~ | ~~SendMessage~~ | — | DROPPED — PR #222 ships it | — |

**Total revised effort:** ~7-8 engineering days. Down from ~10-11 in original 8-pick plan.

---

## Execution preconditions (gates)

Before starting any sub-project:

1. **Phase 0 runs to completion** — all 9 tasks (0.1–0.9) commit a `DECISIONS.md` to `prep/openclaw-tier1-decisions` branch. Each Phase 0 finding that contradicts the plan becomes an amendment HERE before code starts.
2. **`gh pr list` re-checked** — ensure no new PR (since the audit was written) collides on files we plan to touch.
3. **API grep pass (Task 0.9)** runs and produces a "no surprises" report.
4. **archit-2 PII status** — confirm whether #230 has merged. If yes, rebase Sub-project 1.A's branch from latest `main` so the chunker composes correctly with the redaction layer (audit Q3).

If any of these four reveal a new defect, this AMENDMENTS doc gets a new entry before code is written.

---

## Final readiness verdict

**Plan + spec + amendments + Phase 0 actually-run = GREEN. Without Phase 0 actually-run = YELLOW. Original plan as written = RED.**

Recommended next action: open the prep branch, run all 9 Phase 0 tasks, commit `DECISIONS.md`, integrate findings as amendments, **then** start sub-projects in parallel.
