# Wave 5 Deferrals — Design Spec

**Date:** 2026-05-04
**Status:** Draft → ready for review
**Source PRs:** #420 (Wave 5 — `ebe93c18`) + #421 (Telegram T11 + flakes — `ef07fe28`)
**Reference deferrals:** memory `project_hermes_wave5_done.md`

---

## 1. Goal

Ship the 4 still-pending Wave 5 deferrals as one focused PR, 4 self-contained commits. Each commit closes one of the followups intentionally split out of #420.

**Note (revised 2026-05-04):** T11 Telegram override shipped in PR #421. T11 for Discord/Slack/Mattermost/Email/Signal is BLOCKED — those adapters lack `PHOTO_OUT` capability today, so adding `send_multiple_images` overrides without first adding base photo support would be premature. Out of scope for this PR; needs a separate "add PHOTO_OUT to non-Telegram adapters" track.

---

## 2. Scope (Approach B — pragmatic, 4 items)

| # | Deferral | Scope decision |
|---|---|---|
| 1 | **T17 lazy session** | FULL — migrate loop's eager `create_session` → lazy `ensure_session` |
| 2 | **T2 continuation loop** | FULL — end-of-turn `should_continue` + `build_continuation_prompt` injection in `run_conversation` |
| 3 | **T4 `/footer` toggle persistence** | MINIMAL — write the one config key directly via `config_store`; no SlashContext ABC refactor |
| 4 | **T5 OpenRouter cache wiring** | FULL — inject `build_or_headers` into request flow + parse `parse_cache_status` in response |
| ~~5~~ | ~~T11 send_multiple_images Discord+Slack~~ | **DROPPED** — Discord/Slack adapters lack `PHOTO_OUT` capability; native batch overrides without base photo support would be backwards. Needs separate PHOTO_OUT-addition PR first. |

---

## 3. Detailed designs

### 3.1 T17 lazy session creation

**Problem:** `agent/loop.py:691` eagerly calls `self.db.create_session(...)` at the start of every `run_conversation()`. If the conversation never produces a message (user hits Ctrl-C immediately, or a slash-command-only turn returns early), an empty session row pollutes the DB. `auto_prune` cleans these up later but they're noise in `oc sessions list` until then.

**Approach:** Replace the eager `create_session(...)` at line 691 with a one-shot lazy gate. The session row is inserted on first `append_message(sid, ...)` call inside `run_conversation`.

**Implementation shape:**
- Remove the early `create_session(...)` call at line 691.
- Add a small helper `_ensure_session_persisted(sid)` to `AgentLoop` that calls `db.ensure_session(...)` at most once per `run_conversation`. State tracked in `self._session_ensured: set[str]` (per-session-id, idempotent).
- Wrap each `db.append_message(sid, ...)` callsite with a preceding `self._ensure_session_persisted(sid)` call. There are ~10 such sites; a one-line guard before each is preferable to a context manager (no nested-async risk).
- The `initial_messages` batch path (line 704) gets the same guard before its `append_messages_batch`.

**Why a per-loop set instead of per-call lookup?** `db.ensure_session()` is `ON CONFLICT DO NOTHING` so it's idempotent at the DB layer, but each call still pays a transaction cost. Caching the "already ensured" set in memory means subsequent appends in the same conversation skip the round-trip.

**Files:**
- Modify: `opencomputer/agent/loop.py` (remove eager call, add `_ensure_session_persisted`, wrap ~10 callsites)
- Test: `tests/test_loop_lazy_session.py`

### 3.2 T2 continuation-loop wiring

**Problem:** `goal.py` exposes `GoalState.should_continue()`, `build_continuation_prompt()`, `_call_judge_model()`. None of these are called from `run_conversation`. The Ralph loop is dormant.

**Approach:** After the assistant's final message lands in `run_conversation` (just before the return), check `db.get_session_goal(sid)`. If `goal and goal.should_continue()`:
1. Call `judge_satisfied(goal.text, last_assistant_text)` (wraps `_call_judge_model` with retry/fail-open).
2. If NOT satisfied: `db.update_session_goal(sid, goal_turns_used=goal.turns_used + 1)`, build `continuation_prompt = build_continuation_prompt(goal.text)`, and **recursively invoke** `run_conversation(continuation_prompt, session_id=sid, ...)`.
3. If SATISFIED or `should_continue()` false: just return as today.

**Why recursive over a wrapper while-loop?** Recursive call inherits all the existing turn machinery (compaction, tool dispatch, hooks, runtime). A wrapper loop would duplicate that surface. Stack depth bounded by `goal.budget` (default 20) — well below Python's recursion limit.

**Edge cases:**
- Real user message during a continuation should preempt: handled because the continuation only fires AFTER the current turn's return, not concurrently. If the user types something, it takes the next turn naturally.
- Judge call failure: `_call_judge_model` already wraps in try/except — treat as NOT_SATISFIED (fail-open: continue toward goal). Documented in goal.py.
- Goal cleared mid-conversation: re-read `get_session_goal` per iteration — if the row was deleted, `goal is None` and we exit.

**Files:**
- Modify: `opencomputer/agent/loop.py` (add post-turn continuation gate at end of `run_conversation`)
- Modify: `opencomputer/agent/goal.py` (add `judge_satisfied(goal_text, last_response)` wrapper that calls `_call_judge_model` and parses the SATISFIED/NOT_SATISFIED response)
- Test: `tests/test_loop_continuation.py`

### 3.3 T4 `/footer` toggle persistence (minimal)

**Problem:** `/footer` reports current state but cannot persist a toggle because the existing `SlashContext` doesn't expose a config-write helper. The deferral note suggested adding `SlashContext.persist_config()` to the ABC — that would touch every plugin's slash interface.

**Approach (minimal):** Don't add to the ABC. The `/footer` slash handler imports `opencomputer.agent.config_store` directly and uses `load_config()` + `save_config()` to flip the single key. This is the same pattern used by other persistent slash commands in the codebase.

**Tradeoff:** Each slash command that needs persistence currently re-implements this. A future ABC-level refactor can DRY this up. For one slash command, the duplication is acceptable.

**Implementation shape:**
- Add `/footer on|off|show` arg parsing to the existing `/footer` handler.
- For `on` / `off`: load current config, set `cli_ui.show_footer = (args == "on")`, save.
- For `show` (or no args): print current persisted value.
- Reload runtime: also update `runtime.custom["show_footer"]` so the in-memory toggle reflects immediately without restart.

**Files:**
- Modify: the file containing `FooterCommand` (TBD via grep — likely `opencomputer/agent/slash_commands_impl/footer_cmd.py` or in `slash_handlers.py`)
- Modify: `opencomputer/agent/config.py` if `cli_ui.show_footer` field doesn't exist
- Test: `tests/test_footer_persistence.py`

### 3.4 T5 OpenRouter cache header wiring

**Problem:** `extensions/openrouter-provider/provider.py` defines `build_or_headers(cfg)` and `parse_cache_status(headers)` as helpers but never calls them in the actual request/response path. The provider sends requests without the OR cache headers, and never parses cache status from the response.

**Approach:**
- In `OpenRouterProvider.complete()` and `stream_complete()`: call `build_or_headers(self.config)` and merge the result into the `headers` dict passed to httpx.
- After response: extract response headers, call `parse_cache_status(response.headers)`, surface as `Usage.cache_read_tokens` / `cache_write_tokens` (the existing fields on `Usage`).

**Cache-token mapping:** OpenRouter returns `x-ratelimit-cache-status: hit|miss|partial` (per existing helper). On hit: `cache_read_tokens = response_usage.prompt_tokens` (full prompt was a cache hit). On miss: both zero. On partial: leave as zero for now (no per-token breakdown from OR yet).

**Files:**
- Modify: `extensions/openrouter-provider/provider.py` (wire helpers into request + response paths)
- Test: `tests/test_openrouter_cache_wiring.py`

### 3.5 ~~T11 send_multiple_images Discord + Slack~~ — DROPPED

After PR #421 merged Telegram T11, the remaining T11 deferrals are blocked on a prerequisite: Discord/Slack/Mattermost/Email/Signal adapters do not declare `ChannelCapabilities.PHOTO_OUT` today. Adding `send_multiple_images` overrides on adapters that don't support single-image send would be backwards.

The right path is a separate PR that:
1. Adds `PHOTO_OUT` to Discord adapter capabilities + implements single-image `send_image` via `discord.File`
2. Adds `PHOTO_OUT` to Slack adapter capabilities + implements single-image via `files_upload_v2`
3. THEN adds `send_multiple_images` overrides as native-batch optimizations

Documented in the wave5 followup memory; out of scope for this PR.

---

## 4. Architecture diagram

```
┌──────────────────────────────────────────────────────────────┐
│ run_conversation(user_msg, session_id=sid)                   │
│                                                              │
│  T17:  remove eager db.create_session(sid) at line 691       │
│        ───────────────────────────────────                   │
│  before each db.append_message(sid, ...):                    │
│    self._ensure_session_persisted(sid)                       │
│    └─→ if sid not in self._session_ensured:                  │
│           db.ensure_session(sid, platform=, model=, ...)     │
│           self._session_ensured.add(sid)                     │
│                                                              │
│  ... existing loop runs ...                                  │
│                                                              │
│  T2:   at end-of-turn, before return:                        │
│  ──────────────────────────────────                          │
│    goal = db.get_session_goal(sid)                           │
│    if goal and goal.should_continue():                       │
│      satisfied = await judge_satisfied(goal.text,            │
│                                        last_assistant_text)  │
│      if not satisfied:                                       │
│        db.update_session_goal(sid,                           │
│                               goal_turns_used=goal+1)        │
│        cont = build_continuation_prompt(goal.text)           │
│        return await self.run_conversation(cont, sid=sid)     │
│      # else: goal satisfied → return normally                │
└──────────────────────────────────────────────────────────────┘

────────────────────────────────────────────────────────────────

┌──────────────────────────────────────────┐
│ /footer on│off│show                       │
│                                          │
│ T4:  load_config() → mutate              │
│      cli_ui.show_footer = (args=='on')   │
│      save_config(cfg)                    │
│      runtime.custom['show_footer'] = ... │
└──────────────────────────────────────────┘

────────────────────────────────────────────────────────────────

┌──────────────────────────────────────────────────┐
│ OpenRouterProvider.complete()                    │
│                                                  │
│ T5:  headers = {**base_headers,                  │
│                 **build_or_headers(self.config)} │
│      ... POST ...                                │
│      cache_status = parse_cache_status(          │
│        resp.headers)                             │
│      usage.cache_read_tokens = (                 │
│        prompt_tokens if 'hit' else 0)            │
└──────────────────────────────────────────────────┘

────────────────────────────────────────────────────────────────

┌──────────────────────────────────────────────────┐
│ DiscordChannelAdapter.send_multiple_images       │
│   if len(images) <= 10:                          │
│     channel.send(content=text,                   │
│                  files=[discord.File(p,name=n)…])│
│   else: super().send_multiple_images(...) chunk  │
│                                                  │
│ SlackChannelAdapter.send_multiple_images         │
│   client.files_upload_v2(                        │
│     channel=ch,                                  │
│     initial_comment=text,                        │
│     file_uploads=[                               │
│       {file: p, filename: n} for p,n in imgs])   │
└──────────────────────────────────────────────────┘
```

---

## 5. Testing strategy

| Item | Test approach |
|------|---------------|
| T17 lazy session | Unit: `_ensure_session_persisted(sid)` called twice for same sid → DB INSERT issued once. Slash-only turn (returns before append_message) → no row inserted. First message turn → row inserted exactly once before append. |
| T2 continuation | Unit: monkey-patch `judge_satisfied` to return False once then True. Invoke `run_conversation` with active goal → assistant turn fires twice (continuation + final). Goal cleared mid-loop → exits cleanly. Budget exhausted → exits without judge call. |
| T4 /footer | Unit: `/footer on` writes `cli_ui.show_footer=True` to config.yaml. `/footer off` writes False. `/footer show` reads current value. Round-trip: write → load → assert. |
| T5 OpenRouter cache | Unit: mock httpx, capture headers — must include `HTTP-Referer` + `X-Title` from `build_or_headers`. Mock response with `x-ratelimit-cache-status: hit` → `usage.cache_read_tokens > 0`. With `miss` → `cache_read_tokens == 0`. |
| ~~T11 Discord/Slack~~ | Dropped (prerequisite: PHOTO_OUT capability not yet on those adapters) |

All tests mock external clients (httpx, discord.py, slack_sdk). No live network.

---

## 6. Out of scope (explicitly deferred again)

- T11 Mattermost/Email/Signal native batch overrides — base loop fallback works; defer to follow-up if user demand surfaces.
- T4 `SlashContext.persist_config()` ABC method — would touch every plugin's slash interface; minimal direct-write approach gets the immediate value without that surface change.
- T17 ghost-session cleanup heuristic improvements — `auto_prune` already handles old empty rows.

---

## 7. Self-audit (executed before showing this design)

### What might be wrong with this scope?

- **Risk: T2 continuation recursion could blow stack.** Counter: budget is 20 (default), Python default recursion limit is 1000. Even with budget=200 we'd be fine. But `RecursionError` should be caught at the outermost call as a final safety net.
- **Risk: T17 wrapping ~10 callsites is brittle.** Counter: a context manager around `run_conversation` would be cleaner but introduces async-context complexity. The 10 wraps are mechanical; a unit test verifies only one DB INSERT regardless of message count.
- **Risk: Discord 10-file limit isn't constant.** Counter: it's been stable for years; if it changes, base loop fallback covers correctness (chunked sends still work).
- **Risk: Slack `files_upload_v2` requires async for newer SDK; OC's slack adapter version may differ.** Counter: pin the call shape to the SDK version OC actually uses; tests verify against that pin.
- **Risk: T5 cache-token mapping is naive on `partial`.** Counter: documented as "partial → 0 for now"; future PR can add finer breakdown when OR exposes it.

### What edge cases might bite?

1. **T17:** `delegate.py` spawns subagents that call `run_conversation` recursively with the parent's session_id. The lazy-ensure set must be per-AgentLoop-instance, not per-session-id, OR keyed `(sid, depth)` to avoid cross-pollution. Per-instance set is simpler and correct.
2. **T2:** Continuation injection happens at end-of-turn AFTER `db.append_message(assistant_msg)`. This means the recursive call sees the assistant turn already in history — correct. The continuation prompt is appended AS the next user message via `run_conversation(cont, ...)`, which calls `db.append_message(sid, user_msg)` at line 756 — no double-write.
3. **T4:** Concurrent `/footer on` from two channels at once → last-write-wins on config.yaml (file isn't lock-protected today). Acceptable; matches existing config-write behavior.
4. **T5:** OpenRouter response headers may be lowercase OR mixed case. `parse_cache_status` already handles both (uses `.lower()`). Verified.
5. **T11 Discord:** `discord.File` requires a path or file-like object. OC's image attachments are byte buffers — need `io.BytesIO(buf)` wrap. Test must use a real BytesIO, not a path string.

### Was anything missed from the deferral list?

Re-checking against the Wave 5 PR #420 deferrals memory:
- ✓ T2 continuation loop — covered
- ✓ T4 /footer persistence — covered (minimal)
- ✓ T5 OpenRouter wiring — covered
- ✓ T11 platform overrides — covered (Discord + Slack; Mattermost/Email/Signal explicit-skip)
- ✓ T17 lazy session — covered

### Defensible? Yes.

5 commits, 5 self-contained changes, all addressing real Wave 5 deferrals with no scope creep. Each ≤200 LOC. Total estimated time: 4-6 hours.
