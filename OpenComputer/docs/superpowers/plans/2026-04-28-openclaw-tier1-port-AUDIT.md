# OpenClaw Tier 1 Port — AUDIT

**Audited:** 2026-04-28 against `main`@`4db74443` + 12 open PRs (#220-#236).
**Verdict (preview):** RED. The plan ships in its current form with at least 8 hard API breakages (3 of them silent), plus one unflagged file-collision with PR #222.

---

## 1. CRITICAL defects (must fix before execution)

### C1. `outgoing_queue.put_send(channel, peer, message)` does not exist

- **Where:** Plan Sub-projects 1.H (Task H1) and 1.F (`SessionsSend.put_session_send`); spec §4 1.H, §4 1.F.
- **Defect:** The plan calls `outgoing_queue.put_send(...)` and `outgoing_queue.put_session_send(...)`. The real API on `opencomputer/gateway/outgoing_queue.py:142` is `enqueue(*, platform, chat_id, body, attachments=None, metadata=None) -> OutgoingMessage`. There is no `put_send`, no `put_session_send`, no `peer`/`message` parameters. `loader.py:709` confirms "access it via `api.outgoing_queue.enqueue(...)`".
- **Why it matters:** every test and runtime call in 1.H and `SessionsSend` (1.F) raises `AttributeError` on first use. Silent at unit-test level (the plan stubs `_StubQueue.put_send`) but breaks in production smoke-test.
- **Fix:** rename to `enqueue`; rename `channel`→`platform`, `peer`→`chat_id`, `message`→`body`. Drop `put_session_send` entirely (sibling sessions read SessionDB directly; there is no cross-session queue API today — see C8).

### C2. `HookDecision.modified_message` is NOT honored for `PRE_LLM_CALL`

- **Where:** Sub-project B core premise; plan line 7, line 38, Task B3.
- **Defect:** Plan claims "Reuses existing `PreLLMCall` hook + `HookDecision.modified_message`". The actual emit in `opencomputer/agent/loop.py:1825-1841` is `engine.fire_and_forget(...)` — fire-and-forget — and an in-source comment at `loop.py:560-564` states verbatim: "modified_message support for appending a system reminder is documented in the SDK; the loop does NOT consume it today (template author owns the body). A future PR can splice modified_message into the rendered snapshot per the plan." Same for `PRE_LLM_CALL`: returns are "intentionally ignored: this is an observation event, not a gate".
- **Why it matters:** Sub-project B is wholly non-functional. The hook fires; its return value is discarded. The `<relevant-memories>` block is never injected. All B3 unit tests pass (they don't run a real loop) and the user sees zero behavior change. This is a SILENT shipping defect.
- **Fix:** Phase 0.1 must run BEFORE B-anything; if Outcome B (which we now know is the truth), Task B4 becomes mandatory and Sub-project B's effort estimate jumps from M (~2d) to L (~3-4d). Splicing has to: (a) collect the awaited HookDecisions (engine becomes non-fire-and-forget for this event, OR a parallel `await emit(...)` path is added), (b) inject either as a `system` message (Anthropic accepts a system parameter, not a "user" role with `<system-reminder>`), (c) document that PreLLMCall is now blocking, (d) update every existing PreLLMCall handler audit.

### C3. `CredentialPool` API is profile-key-string, not profile_id

- **Where:** Sub-project E (E1, E2), spec §4 1.E.
- **Defect:** Plan creates/extends `plugin_sdk/credential_pool.CredentialPool.cooldown(profile_id, seconds)` and `available_profiles()`. Real module is at `opencomputer/agent/credential_pool.py` (NOT plugin_sdk). Real surface is `acquire() -> str (key)`, `report_auth_failure(key, reason)`, `with_retry(fn, is_auth_failure)`. Keys are API-key strings, NOT profile IDs. There is no `profiles=[...]` constructor arg, no `available_profiles()`, no `all_profiles()`.
- **Why it matters:** plan E.1 imports a path that doesn't exist and constructs the class with kwargs the class doesn't accept — every E.1 test fails at import. E.2 calls `self.credential_pool.cooldown(self.current_profile_id, ...)` — neither attribute exists on existing providers.
- **Fix:** Either (a) accept that "profile" semantics don't exist and reframe E as adding cooldown to the existing per-key quarantine (the existing `report_auth_failure` already quarantines for `rotate_cooldown_seconds=ROTATE_COOLDOWN_SECONDS`), making the whole sub-project a thin CLI/config surface for what's already there; OR (b) introduce a profile-aware abstraction *first* (separate sub-project; this becomes 2 PRs).

### C4. `ToolCall.input` and `ToolResult.output` do not exist

- **Where:** Sub-projects F, G, H (every tool implementation + every test).
- **Defect:** All 7 new tools use `call.input["..."]` and `ToolResult(output=..., is_error=...)`. Real shape (`plugin_sdk/core.py:62-77`): `ToolCall(id: str, name: str, arguments: dict[str, Any])` and `ToolResult(tool_call_id: str, content: str, is_error: bool)`.
- **Why it matters:** every tool's `run()` raises `KeyError`/`TypeError` on first call. Every test that constructs `ToolCall(name=..., input=...)` fails at frozen-dataclass init (no `input` kwarg).
- **Fix:** global rename across F/G/H — `call.input` → `call.arguments`, `ToolResult(output=X, is_error=Y)` → `ToolResult(tool_call_id=call.id, content=str(X), is_error=Y)`. Same fix in every test fixture. Audit existing tools in `opencomputer/tools/` for the canonical pattern.

### C5. `SessionDB.get_messages` does NOT take `limit=`; `get_session_summary` does not exist

- **Where:** Sub-project F — `SessionsHistory` and `SessionsStatus`.
- **Defect:** `state.py:811` is `def get_messages(self, session_id: str) -> list[Message]:` — no `limit` parameter. There is no `get_session_summary` method anywhere in `state.py` (`get_session(session_id) -> dict | None` exists; that may be the intended target). `last_tool_used` field is not in `list_sessions()` rows either.
- **Why it matters:** SessionsHistory raises `TypeError` on first call. SessionsStatus raises `AttributeError`. Unit tests in the plan are stubbed out (`...  # engineer fills in`), so this lands silently and breaks in smoke-test.
- **Fix:** add `limit: int | None = None` param to `get_messages` (fan-out: every existing caller must stay stable) and slice in the tool, OR slice client-side in `SessionsHistory`. Either rename `get_session_summary` to `get_session` and accept the narrower shape, or add a real summary method.

### C6. PR #222 (open) ALREADY ships `opencomputer/tools/send_message.py`

- **Where:** Sub-project H file plan + spec §6 conflict map.
- **Defect:** Plan §6 claims "Other 8 PRs (#220-#228) — verified disjoint in earlier phases". `gh pr view 222` shows it adds `opencomputer/tools/send_message.py` and `tests/first_class_tools/test_send_message_tool.py`. This is the same file Sub-project H plans to create.
- **Why it matters:** whichever lands second hits a hard merge conflict. The "verified disjoint" claim is false. (PR #222 also adds `mixture_of_agents.py`, `vision_analyze.py`, `image_generate.py` — touches `cli.py` registration in the same vicinity.)
- **Fix:** drop Sub-project H entirely (PR #222 covers it) OR rebase H onto `feat/first-class-tools` and re-scope to the deltas only.

### C7. `BaseChannelAdapter.send` signature mismatch in 1.A wrapper

- **Where:** Sub-project A Task A3 — `_maybe_chunk_delta` calls `self.send(chat_id, delta)`.
- **Defect:** Real signature (`channel_contract.py:213`) is `async def send(self, chat_id: str, text: str, **kwargs) -> SendResult` and returns a typed `SendResult`. The plan ignores the return value — fine — but the actual streaming path uses `edit_message` (in-place message updating) for Telegram/Discord/Slack, NOT `send` (which creates new messages). Wrapping deltas via `send` produces N separate messages for one streamed reply, not in-place edits.
- **Why it matters:** the chunker becomes a regression on Telegram (currently one edited message becomes N new messages — UX worse than today).
- **Fix:** Phase 0.2 is critical; the wrapper must wrap `edit_message` (or accept a `send_or_edit` callable injected by dispatch). The plan says "Option α (dispatch-level) — the cleaner pattern" — pick that explicitly and write `_maybe_chunk_delta` against the dispatch's `delta_handler`, not `send`.

### C8. `agent_runner.spawn_async` does not exist; `runner=agent_runner` in `cli.py` has no source

- **Where:** Sub-project F Task F1-F5 — `SessionsSpawn` constructor + cli.py registration.
- **Defect:** Plan registers `SessionsSpawn(db, runner=agent_runner)` and calls `await self.runner.spawn_async(new_sid, prompt, model=model)`. Searching: there is no `agent_runner` symbol in `cli.py` and no `spawn_async` method on `AgentLoop`. SessionDB is per-process; cross-process spawn is a non-trivial gateway problem and is NOT a 1-task add.
- **Why it matters:** F1's test uses the stub fixture pattern (`...`) so it passes; cli.py registration fails at import; the smoke test "SessionsSpawn returns new session_id" is a fantasy.
- **Fix:** Either (a) restrict `SessionsSpawn` to creating a SessionDB row + queuing a "first message" via an existing channel — no spawn — and document this is "session bookmarking, not concurrent execution"; OR (b) split off "spawn infrastructure" into its own sub-project (M-L effort, not S).

---

## 2. HIGH-priority concerns

### H1. Active Memory plugin imports `from cache import …` and `from runtime import …`

- **Where:** Sub-project B Task B3, plan line 1260-1261.
- **Defect:** Plan does `from cache import DecisionCache` at module top, relying on the loader's sys.path fix. Real loader (`loader.py` + `_PLUGIN_LOCAL_NAMES = ("provider","adapter","plugin","handlers","hooks")`) does NOT include `cache` or `runtime` in the cache-clearing list. Two plugins both naming a sibling `cache.py` would cross-pollute.
- **Why it matters:** if any future plugin (or already-loaded test fixture) registers a `cache` or `runtime` module name first, active-memory imports the wrong class. This is exactly the gotcha CLAUDE.md §7.1 calls out.
- **Fix:** add `cache`, `runtime` to `_PLUGIN_LOCAL_NAMES` in `loader.py:42` as part of B3, OR rename to less collision-prone names (`active_memory_cache.py`, `active_memory_runtime.py`).

### H2. `api.provider`, `api.memory`, `api.slash_commands()`, `api.config`, `api.list_enabled_channels()` — none verified

- **Where:** Sub-project B Task B3 (`api.provider`, `api.memory`, `api.slash_commands().add(...)`, `api.config`); Sub-project H Step 3 (`api.list_enabled_channels()`).
- **Defect:** `registry.py:65` shows `slash_commands` is a plain `dict[str, Any]`, not a method. There is no `provider` attribute on `PluginAPI` (providers are accessed via the registry by name). `api.memory` likely refers to the `memory_provider` slot; not the same as `MemoryManager`. `list_enabled_channels()` is invented.
- **Why it matters:** B3's `register(api)` blows up at `api.provider` access; B5's `api.slash_commands().add(...)` blows up because `dict.add` doesn't exist (it's `__setitem__`). H's `api.list_enabled_channels()` is a fabrication.
- **Fix:** add Phase 0.7 — enumerate `PluginAPI.__dict__` and document the actual attribute names + types. Update B3, B5, H to match. The slash-command registration pattern in the codebase is `api.slash_commands["name"] = handler`, not `api.slash_commands().add(...)`.

### H3. `HookSpec(priority=200)` — verified valid, but 100 is default; 200 means LATER not earlier

- **Where:** Sub-project B Task B3 (registers `priority=200`).
- **Defect:** `hooks.py:122` confirms `priority: int = 100` and "lower priority runs first". Active Memory at 200 means it runs AFTER any default-priority handler. If another PreLLMCall handler (e.g. an audit logger) returns a `modified_message` first, ordering is undefined for "who wins".
- **Why it matters:** with C2 fixed via splicing, the order of `modified_message`s matters. Late-priority handlers append later in the system prompt — possibly losing precedence for memory-recall context.
- **Fix:** document priority-200 rationale ("active memory should be the LAST to inject so its context is closest to the upcoming user turn"); add a regression test asserting two PreLLMCall hooks don't clobber each other.

### H4. `BlockChunker.feed` re-runs full `_extract_one` loop on every call — quadratic on long replies

- **Where:** Sub-project A `BlockChunker.feed` line ~290.
- **Defect:** Each delta appends to `self._buf`; each iteration of `_extract_one` does `buf.find(...)` from `min_chars` over the full buffer. For a 10k-char streamed reply, this is O(N²) in the buffer length.
- **Why it matters:** noticeable lag on long Telegram replies; also makes Telegram's rate-limited edit cadence even slower.
- **Fix:** track `self._scan_from` cursor and pass to `find()`; reset only on emit.

### H5. LoopDetector resets at session boundary but NOT on subagent spawn

- **Where:** Sub-project C Task C2 — `self._loop_detector.reset()` at `run_conversation` start.
- **Defect:** `DelegateTool` spawns subagents that share the parent's `AgentLoop` instance (verify: it doesn't, but the parent's loop_detector state is shared via instance attribute). If the subagent does 3 `Bash` calls and the parent then does 1 more `Bash`, the parent's 1st call gets flagged.
- **Why it matters:** false positives in healthy multi-agent sessions.
- **Fix:** scope the detector to `(session_id, depth)` or push/pop on subagent boundaries.

### H6. Replay sanitization assumes `replay`, `in_flight`, `ts` fields exist on messages

- **Where:** Sub-project D — `sanitize_for_replay`.
- **Defect:** `Message` (in `plugin_sdk/core.py`) has `role`, `content`, `tool_call_id`, `tool_calls`, `name`, plus reasoning fields. There is NO `replay`, `in_flight`, or `ts` attribute. `SessionDB` schema has no such columns.
- **Why it matters:** `m.get("replay")` always returns None on real messages. The function is a no-op in production. Tests use synthetic dicts with these fields and pass — silent shipping defect.
- **Fix:** the schema needs new columns + writers must set them. This is a multi-file change, not a single-file create. Re-scope D from "S, ~half day" to "M, ~1.5d" with schema migration.

### H7. Sub-project H's `enabled_channels` baked in at registration

- **Where:** H Task H1.
- **Defect:** `enabled_channels=enabled` is captured at startup. Channel enable/disable mid-session is supported by OC's demand-driven activation (E6). The baked-in list goes stale.
- **Why it matters:** SendMessage rejects newly-enabled channels until restart.
- **Fix:** read live from `api.list_enabled_channels()` (after H2 fix verifies the method exists) inside `run()`.

---

## 3. MEDIUM observations

- **M1.** Plan's `BoundaryKind` Literal includes `"max"` for force-cut, but the spec §4 1.A omits `"max"` from `prefer_boundaries`. Minor doc drift.
- **M2.** Test fixtures in B/C/D use `...  # engineer fills in` for the integration tests. These are not real tests; they will pass on `pass` but verify nothing.
- **M3.** Plan paths refer to `extensions/active-memory/` with a hyphen; Python imports use underscore. The loader handles this via plugin_id mapping but the imports `from cache import …` rely on filesystem layout, fragile if anything renames.
- **M4.** Sub-project A's `humanDelay` random.uniform() is process-global stateful; a fixed-seed test in CI works but two adapters share the seed unintentionally.
- **M5.** Sub-project E2 catches `httpx.ConnectError`, but `extensions/anthropic-provider` may use the Anthropic SDK's typed exceptions; the import path matters.
- **M6.** `oc plugin enable active-memory` is referenced but the plugin id is "active-memory" while many CLI lookups assume snake_case.

---

## 4. Stress tests

**Q1. All 8 PRs in parallel + cli.py rebase conflicts.** PR #222, #223, #224, #225, #226, #232, #234 all touch `cli.py`. Sub-project F (cli.py registration), G (cli.py), H (cli.py) all add registrations. Realistically: F + G land. H is duplicated by #222. Expect 2-3 manual rebases of cli.py per merge.

**Q2. Phase 0.1 finds modified_message NOT honored (we already know).** Plan grows from 8 to 9+ tasks (Task B4 mandatory; engine signature change; non-fire-and-forget path; audit existing PreLLMCall handlers). Effort jumps M→L. Plan is NOT robust — it casts B4 as "5-10 LOC" but loop.py:1833 `fire_and_forget` is the wrong path; switching to a synchronous-emit-and-collect is real surgery, not a splice.

**Q3. archit-2 PII redaction lands first.** Redaction at dispatch layer happens AFTER the chunker. The chunker emits paragraph blocks; redaction sees paragraph blocks and runs replacement. Should compose. RISK: redaction may not be fence-aware — could mangle code-fence content. Add a regression test in 1.A's PR for "fenced code with PII does not get split AND does not get redacted incorrectly".

**Q4. Model returns `inject` every turn.** TTL=60s, max_entries=256. On a high-traffic group chat with 256 distinct users in 60s, no caching at all → 1 sub-agent call per turn × 8000ms timeout × $0.0002 (haiku) ≈ $0.05/min worst case. Acceptable. But cache key is `(session_id, msg_hash)` which is per-CONVERSATION not per-user — collisions are bounded.

**Q5. LoopDetector + delegate.** See H5. Per-session reset is insufficient. Need per-session-AND-per-depth.

---

## 5. Alternatives

- **1.A — single 30-line wrapper?** No — fence-safety is the point and that's ~80 LOC minimum. KEEP, but address H4 quadratic-feed.
- **1.B — just inject latest 3 RecallTool results unconditionally?** Yes, vastly cheaper (no sub-agent). Loses nuance but ships in 1 day, not 4. Strongly consider.
- **1.C — keep, simplest sub-project.**
- **1.D — skip if "in_flight" markers don't exist (H6); add columns is the real cost.** Reconsider.
- **1.E — drop to "config surface for existing per-key quarantine" (C3 alt-a).**
- **1.F — split. SessionsList/History/Status are read-only and trivial. SessionsSpawn/Send require infra (C8). Ship the read trio first.**
- **1.G — keep. Smallest sub-project.**
- **1.H — DROP. PR #222 ships it (C6).**

---

## 6. Final verdict

| Pick | Verdict | Rationale |
|---|---|---|
| 1.A Block chunker | REVISE | Phase 0.2 mandatory; fix C7 (wrap edit, not send) + H4 quadratic. |
| 1.B Active Memory | REVISE-HEAVY | C2 invalidates premise; either accept 4d effort or drop to "RecallTool-prepend" alternative. |
| 1.C Anti-loop | KEEP | Smallest risk; fix H5 sub-agent scoping. |
| 1.D Replay sanitization | REVISE | H6 schema columns; rescope to M effort. |
| 1.E Auth cooldown | REVISE | C3 — pick alt-a (config surface) or split. |
| 1.F Sessions-* | SPLIT | Read trio (List/History/Status) keep; Spawn/Send defer (C8). Fix C4 + C5. |
| 1.G Clarify | KEEP | Fix C4. |
| 1.H SendMessage | DROP | C6 — PR #222 already ships it. |

**Overall:** **RED.** Three silent-shipping defects (C2, C5, H6), one duplicate PR (C6), one wrong API path (C3), and one wrong attribute model (C4) make execution-as-written guaranteed to break — and most breakages will pass the plan's unit tests (which stub the real surfaces).

**Recommended next action:** rebuild the plan with three preconditions: (1) Phase 0 actually runs and writes the DECISIONS doc BEFORE any branch is cut (current plan front-loads 6 verifications, then ignores them in design); (2) every API call in every code block is grep-verified against `main` (`call.input` → `call.arguments` is mechanical); (3) re-check `gh pr list` for file collisions, not just commit messages. Then re-audit. Until those three land, do NOT start sub-projects.
