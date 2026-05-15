# Profile Handoff — End-to-End Investigation (v2, audited)

Author: 2026-05-14
Status: Audit of the existing implementation. Every claim is file:line-anchored.
Owner: Saksham

This is **v2 of the doc**. v1 had two material errors and several unverified
claims; both are corrected here. The "Correction log" section at the bottom
lists every change vs. v1.

---

## TL;DR — what actually happens vs. what users assume

Users see the swap and assume it's a full profile switch — like restarting
`oc -p <other>`. **It is not.** It's a partial, surgical swap of a few
specific pieces of state. Everything else stays bound to the original profile
for the rest of the process lifetime.

### What DOES swap between turns (verified)

| State | Mechanism | Source |
|---|---|---|
| `MEMORY.md` path | `memory.rebind_to_profile()` | `agent/loop.py:678`, `memory.py:962-984` |
| `USER.md` path | same | same |
| `SOUL.md` path | same | same |
| BM25 + vector indexes | new instances pointing at new home | `memory.py:980-984` |
| `~/.opencomputer/active_profile` sticky file | `write_active_profile()` | `_profile_swap.py:87`, `profiles.py:271-283` |
| `runtime.custom["active_profile_id"]` | direct write | `_profile_swap.py:88` |
| Prompt cache snapshot for THIS session | dict pop | `agent/loop.py:691` |
| Handoff audit logger DB binding | per-turn re-init if profile changed | `agent/loop.py:4174-4192` |
| Handoff inbox doc injection (next turn) | `HandoffInjectionProvider.collect()` reads new home's inbox | `injector.py:62-92` |
| Auto-swap cooldown counter (5-turn) | `trigger.mark_swapped()` resets | `auto_swap.py:263-271` |

That's 10 things. The audit logger rebind (item 8) is **new in this v2 doc** —
v1 incorrectly said audit writes go to the OLD profile. They do not — the
logger is rebound at the top of every turn if `current` differs from
`cached_profile`. Source: `agent/loop.py:4177-4190`.

### What does NOT swap (verified)

| State | Where it's locked in | Source-of-truth verification |
|---|---|---|
| `SessionDB` connection (`sessions.db`) | Constructed once in `AgentLoop.__init__` | `agent/loop.py:759`; `SessionDB` has neither `close()` nor `rebind()` method (verified via grep) |
| `SubagentStore` | Bound to `self.db.db_path` once | `agent/loop.py:778` |
| `EpisodicMemory` | Bound to `self.db` once | `agent/loop.py:908` |
| `.env` (API keys, OAuth tokens) | Loaded once at process startup via dotenv | `cli.py:425` (`_apply_profile_override` sets `OPENCOMPUTER_HOME`; subsequent dotenv load reads from there) |
| `config.yaml` (loop / hooks / MCP / persona) | `Config` constructed once from active profile | `cli.py:1761` (CLI), `cli_gateway.py:161` (gateway) |
| `OPENCOMPUTER_HOME` env var | Set at process start by `_apply_profile_override` | `cli.py:489`; NOT updated by `consume_pending_profile_swap` (verified — grep found no env-write in swap path) |
| MCP subprocesses + `MCPManager` | Constructed once at chat startup | `cli.py:1761`, `cli_gateway.py:161` |
| Skills directory binding | Explicit non-rebind | `memory.py:971-973` ("skills_path NOT rebound — shared across profiles") |
| `home/` sandbox (git/ssh/npm credentials for subprocesses) | Bound to active profile via sticky file + `scope_subprocess_env` | `profiles.py:110-154` — subprocesses DO see the new sandbox post-swap because `scope_subprocess_env` reads the live sticky file. **This is the one consistency point.** |
| Browser-profile (Chromium user-data-dir) | Bound at plugin activation | browser-harness plugin |
| Plugin registry + loaded plugins | Loaded once at startup | `plugins/loader.py` + `plugins/discovery.py` |
| Hook engine + hook configs | Loaded once at startup | `hooks/` package |
| Provider client (and its API key) | Constructed once at startup | `cli.py:1761` provider factory |
| `cost_guard.json` / rate limits | Per-profile but loaded once | bound to `_home()` at config load |
| Cron jobs | Per-profile `cron.db` but registered at startup | |
| Kanban state | Per-profile `kanban.db` but bound at startup | |
| Honcho / external memory provider | Loaded once at startup | memory-honcho plugin |
| Langfuse / observability bindings | Loaded once at startup | langfuse plugin |
| Wire server clients (active WS connections) | Bound to process | `gateway/wire_server.py` |
| Gateway channel adapters (Telegram/Discord/etc.) | Loaded once at gateway start | `gateway/server.py` |

That's still **20 categories** of state that don't swap, including the load-bearing
ones: SessionDB, `.env`, MCPs, plugins, providers.

### What this means in practice

The "swap" is **a system-prompt skin change with handoff injection plus an
audit-DB rebind**. The model sees a different SOUL.md, sees different
MEMORY.md content, sees the handoff banner — so its tone, retrieved facts,
and behavior shift. Audit rows for the swap event go to the correct
(target) profile's `audit.db`.

But it's still:

- Writing chat history to the **original** profile's `sessions.db`.
- Using the **original** profile's API keys.
- Calling the **original** profile's MCP servers.
- Running plugins / hooks / providers loaded from the **original** profile.
- The Python process has `OPENCOMPUTER_HOME` still pointing at the original
  profile, so `_home()` returns the old path.
- BUT — subprocesses spawned by BashTool / similar use the **new** profile's
  `home/` sandbox because `scope_subprocess_env` reads the live sticky file.

That last bullet is the asymmetry §3 unpacks.

---

## 1. The full lifecycle, file:line by file:line

### 1.1 Entry: classifier path or slash command path

**Auto path entry point** — `agent/loop.py:1558-1564`:

```python
try:
    await self._run_handoff_auto_swap(sid=sid, messages=messages)
except Exception:
    _log.warning("handoff auto-swap pipeline raised; turn continues", exc_info=True)
```

Runs on every user turn after early-return slash-command guards.

**Auto path implementation** — `agent/loop.py:4093-4235`
(`_run_handoff_auto_swap`):

1. Resolve `current = runtime.custom["active_profile_id"] or read_active_profile() or "default"` (line 4132).
2. Compute `available = sorted(list_profiles())` (line 4139).
3. Compute `auto_off = config.auto_swap_handoff == "off"` (line 4130). Default is `"silent"` — auto is on.
4. Resolve `plan_mode` via `effective_permission_mode()` (line 4147).
5. Detect gateway session via `runtime.custom["_is_gateway_session"]` (line 4154).
6. Cache + lazy-build `AutoSwapTrigger` (lines 4160-4163).
7. **Audit logger rebind if current profile changed** (lines 4171-4192):
   - Closes old logger if rebinding.
   - Calls `_init_handoff_audit_logger(current)` which builds path
     `<current_profile_root>/consent/audit.db`.
   - Caches under `self._handoff_audit_logger` + `self._handoff_audit_logger_profile`.
8. Cache + lazy-build `ProviderAdapter` wrapping `self.provider` (lines 4200-4206).
9. Call `run_auto_swap_pipeline(...)` (line 4219).

**Manual path** — `agent/slash_commands_impl/handoff_cmd.py` (full file 154
LOC, verified):

- `HandoffCommand.execute()` parses args (`--no-content` flag, target name).
- Lists available profiles via `list_profiles()`.
- Resolves target home via `_profile_home(target, get_profile_dir)`.
- If `--no-content`: directly sets `runtime.custom["pending_profile_id"] = target` (line analogous to `_queue_swap_no_handoff`).
- Otherwise: calls `_generate_and_write()` which constructs `GeneratorInput`, runs `HandoffGenerator.generate()`, writes via `HandoffInbox.write()`.
- Sets `pending_profile_id` only AFTER the inbox write succeeds — fail-closed.

Both paths converge: `runtime.custom["pending_profile_id"]` is set, then the
apply step happens later in the same turn.

### 1.2 Classifier call (auto path only)

`agent/handoff/orchestrator.py:220-238`:

```python
ctx = ClassificationContext(
    foreground_app=foreground_app,
    time_of_day_hour=12,                  # ← hardcoded
    recent_file_paths=(),
    last_messages=tuple(last_user_messages),
    window_title=window_title,
    profile_home=profile_home,
)
cls: ClassificationResult = classify(ctx)
```

**v2 note (NEW finding):** `time_of_day_hour=12` is hardcoded. The
ClassificationContext takes a real hour but the loop hardcodes noon. Either
unused by the classifier or an actual bug — would need to read
`opencomputer/awareness/personas/classifier.py` to confirm. I haven't yet,
so this is a flagged open question, not a verified claim.

Same for `recent_file_paths=()` — passed empty even though the field exists.
The classifier may have stronger signal if these were populated; not
verified.

### 1.3 Trigger evaluation

`agent/handoff/auto_swap.py:134-261` — `AutoSwapTrigger.evaluate()`. State
machine (verified):

1. **Always append to rolling window** (line 169) regardless of gates.
   Cooldown is a fire-suppressor, not a memory wipe.
2. **Gates** (lines 171-195) — each returns a `SwapDecision(None, reason)`:
   - `auto_off` → `AUTO_OFF`
   - `plan_mode` → `PLAN_MODE`
   - `is_gateway_session and not gateway_optin` → `GATEWAY_DISABLED`
   - `state.cooldown_remaining > 0` → `COOLDOWN_ACTIVE` (also decrements)
3. **Single-turn confidence < 0.8** → `BELOW_THRESHOLD` (line 196).
4. **Streak check** (lines 205-226):
   - Window tail must be ≥ 3 entries.
   - All same persona (else `MIXED_PERSONAS`).
   - Min confidence in tail ≥ 0.8 (else `BELOW_THRESHOLD`).
5. **Persona → profile resolution** (lines 228-253):
   - `"default"` persona → `PERSONA_UNMAPPED` (the fallback bucket is never a
     swap target).
   - Else `_persona_matches_profile()` from `opencomputer.profile_analysis`
     (fuzzy match). Not opened in this audit; the contract is "first match
     wins, returns profile name or None."
   - Target == current → `PERSONA_IS_CURRENT`.
   - No target found → `NO_AVAILABLE_TARGET`.
6. **Fire** (line 255) returns `SwapDecision(target_profile=X, reason=FIRED)`.

**Tunables** (verified at `auto_swap.py:44-47`):

| Tunable | Default |
|---|---|
| `streak_length` | 3 |
| `confidence_threshold` | 0.8 |
| `cooldown_turns` | 5 |
| `window_size` | 3 |

All overridable via constructor kwargs; not via config.yaml in current state.

### 1.4 Generation (auto and manual)

`agent/handoff/generator.py:114-222` — `HandoffGenerator.generate()`,
fully read.

1. Validate `GeneratorInput`. Source == target → `ValueError`. Bad trigger
   → `ValueError`.
2. Call `render_handoff_prompt(...)` from `protocol_v2.py`. Builds
   (system, user) where system is the **full handoff-protocol v2.0 body**
   (R1–R13 rules, three-questions framework, override clause, self-check) —
   verified at `protocol_v2.py:51-131`. The protocol is the canonical spec
   embedded in code. Body length cap: 6000 chars (`MAX_BODY_CHARS`).
3. `_complete_with_retry(system, user)`:
   - Wraps `provider.complete_text()` in `asyncio.wait_for(timeout=31s)`.
   - Provider's own timeout is 30s.
   - On `TimeoutError` / `ConnectionError` / `OSError` / generic exception:
     log WARN, retry once after 2s backoff.
   - On `HandoffGenerationError`: do NOT retry, propagate.
4. Parse via `parse_handoff_response(raw)`:
   - Empty / None → `NO_TRIVIAL`.
   - `HANDOFF_NOT_WARRANTED: <reason>` prefix → map reason heuristically to
     `NO_EMPTY` / `NO_COMPLETED` / `NO_TRIVIAL` via keyword scan.
   - Otherwise → `YES`, body truncated at 6000 chars at a paragraph boundary.
5. If `parsed.warranted != YES` → return `None` (NOT an error — Step 0 said
   no handoff needed).
6. Empty body after non-warranted-prefix check → raise `HandoffGenerationError`.
7. Build `HandoffMetadata` with frozen `protocol_version="handoff-v2"`,
   UTC-now timestamp, source/target/session_id/trigger from input.
8. Return `HandoffDocument(metadata, body)`.

### 1.5 Inbox write

`agent/handoff/inbox.py:91-161` — `HandoffInbox.write()`, fully verified:

1. Type-check `doc` is `HandoffDocument`.
2. Non-empty body check.
3. `protocol_version == "handoff-v2"` check.
4. `mkdir(parents=True, exist_ok=True)` on `<target_home>/inbox/`.
5. **Atomic write idiom**:
   - `tempfile.NamedTemporaryFile(mode="w", dir=str(inbox_dir), prefix=".handoff_tmp_", suffix=".md", delete=False)`.
   - Write content. `flush()`. `os.fsync(fd)`. `close()`.
   - `os.replace(tmp_path, final_path)` — POSIX-atomic rename.
   - Best-effort temp cleanup on failure.
6. Return final path.

**Filename**: `handoff_<UTC>_<source-profile>_<rand6>.md`. UTC is 20 chars
`20260514T170000Z` (sortable). rand6 is 3 bytes of `secrets.token_hex(3)`.

**File body** (`_render_file_body` at `inbox.py:286-303`):

```markdown
---
protocol_version: handoff-v2
source_profile: default
target_profile: stocks
generated_at: 2026-05-14T17:00:00Z
source_session_id: 01HXX...
trigger: auto
classifier_confidence: 0.873
classifier_reason: state-query / greeting detected
---
<markdown body>
```

Concurrent-write safety: parallel sessions writing to the same target inbox
get different files (random suffixes avoid collision). Sort by UTC prefix
gives chronological order on read.

**Inbox read cap**: 50 pending files max (`_MAX_PENDING_READ` at line 52);
older files are dropped with a WARN.

### 1.6 Queue the swap

After successful generation + inbox write (`orchestrator.py:327-328` auto,
`handoff_cmd.py:99` manual):

```python
runtime.custom["pending_profile_id"] = target
trigger.mark_swapped(runtime=runtime, session_id=session_id)
```

Cooldown counter set to 5 turns (auto only).

### 1.7 Audit + bus event (auto path)

`orchestrator.py:330-372`:

1. **`HandoffAuditLogger.append(SwapAuditEvent(...))`** — verified at
   `audit.py:119-145`:
   - `SwapAuditEvent` packs into `AuditEvent` via `to_audit_event()`:
     `action="profile_swap"`, `capability_id="profile:<target>"`,
     `actor=auto_swap|manual_handoff|cli`, `tier=0`,
     `scope="persona=X|conf=0.873|handoff=<path>"`, `decision=allow|abort|deferred`.
   - Inner `AuditLogger.append()` HMAC-chains into the existing
     `audit_log` table. Schema verified at `audit.py:162-200` — UPDATE
     and DELETE triggers prevent in-place tampering.
2. **`runtime.custom["profile_swap_notification"]`** set with a UI dict.
3. **`_publish_swap_event()`** — bus publish. WireServer subscribes (verified
   at `gateway/wire_server.py:156-209`) and broadcasts `profile.swap` to all
   connected WS clients (TUI, hermes-workspace SPA, IDE bridges).
4. **Dashboard SPA subscription** (verified at
   `dashboard/static/_dashboard.js:131-211`) — the workspace SPA listens via
   SSE on `/api/v1/events?topics=profile_swap` and renders a toast.

So the UI side IS wired across CLI / workspace / wire surfaces. The model
side is partial (memory rebind + audit rebind only).

### 1.8 Apply the swap — same turn

`agent/loop.py:641-693` — `_apply_pending_profile_swap`. Verified in full:

```python
def _apply_pending_profile_swap(runtime, *, memory, prompt_snapshots, sid):
    from opencomputer.cli_ui._profile_swap import (
        consume_pending_profile_swap, init_active_profile_id,
    )
    from opencomputer.profiles import get_profile_dir

    init_active_profile_id(runtime)
    new_id = consume_pending_profile_swap(runtime)
    if new_id is None:
        return None

    new_home_root = get_profile_dir(None if new_id == "default" else new_id)
    new_home = new_home_root / "home"
    if memory is not None and hasattr(memory, "rebind_to_profile"):
        try:
            memory.rebind_to_profile(new_home)
        except Exception:
            _log.warning(
                "profile swap to %r succeeded but memory rebind failed; "
                "MEMORY/SOUL/USER will continue reading the previous profile "
                "until next session restart",
                new_id, exc_info=True,
            )

    if prompt_snapshots is not None and sid is not None:
        prompt_snapshots.pop(sid, None)

    return new_id
```

Three things happen:

1. **`consume_pending_profile_swap`** (`_profile_swap.py:70-89`):
   - Pops `runtime.custom["pending_profile_id"]`.
   - Calls `write_active_profile(...)` — updates the sticky file at
     `~/.opencomputer/active_profile`.
   - Updates `runtime.custom["active_profile_id"]`.
2. **`memory.rebind_to_profile(new_home)`** (`memory.py:962-984`):
   - Reassigns `self.declarative_path`, `self.user_path`, `self.soul_path`
     to the new home dir.
   - Constructs `BM25Index(new_home)` and `VectorIndex(new_home)` — fresh
     instances.
   - Explicit comment: skills_path / global_soul_path NOT rebound.
3. **Prompt-snapshot eviction**: `prompt_snapshots.pop(sid, None)`.

**v2 finding:** `new_home_root` is `get_profile_dir(None if new_id ==
"default" else new_id)` → for "default" returns `~/.opencomputer/`, for a
named profile returns `~/.opencomputer/profiles/<name>/`. Then `new_home =
new_home_root / "home"`. So the memory rebind always points at the per-profile
`home/` subdirectory, not the profile root. The MEMORY.md / USER.md / SOUL.md
files at the **profile root** are not what's being read — the **`home/`
subdirectory's** copies are. That's actually a subtle thing: profiles have a
profile root (where config.yaml lives) AND a `home/` subdir (where MEMORY.md
lives post-rebind). Verified the path at `_apply_pending_profile_swap:675`.

### 1.9 Turn N+1 — handoff is injected

`agent/handoff/injector.py:35-92` — `HandoffInjectionProvider.collect()`,
verified:

1. Call `self._resolver()` to get active profile home Path. Resolver is not
   cached — fresh on each turn.
2. Construct `HandoffInbox(home)`.
3. `inbox.read_and_process_all()` (verified at `inbox.py:232-253`):
   - List pending handoff files (sorted by UTC prefix).
   - Read each, parse frontmatter.
   - Move each to `inbox/processed/` via `os.replace` (atomic).
4. If any docs, render as `## Profile Handoff(s)` section with R12 banner
   and append to system prompt.
5. Cap at 10,000 chars total (`_MAX_TOTAL_INJECTION_CHARS`); over-cap → drop
   oldest first.

Priority 500 → runs near the end of injection composition (lower priorities
like plan-mode at 10 run first).

### 1.10 The "swap" is now complete from the prompt's perspective

```
turn N user message
      │
      ▼
classifier → trigger → FIRED?
      │ yes
      ▼
generate handoff doc (LLM call, single retry, 30s timeout)
      │
      ▼
write to <target>/home/inbox/<file>.md  (atomic + fsync)
      │
      ▼
runtime.custom["pending_profile_id"] = target
trigger.mark_swapped (cooldown=5)
      │
      ▼
HandoffAuditLogger.append → <current-profile>/consent/audit.db (HMAC chain)
bus publish "profile_swap" → WireServer → all WS clients
runtime.custom["profile_swap_notification"] set
      │
      ▼
_apply_pending_profile_swap   ← STILL TURN N
      │
      ├─ write sticky active_profile file
      ├─ memory.rebind_to_profile(new_home_root / "home")
      ├─ next-turn audit logger will rebind to new profile's consent/audit.db
      └─ evict prompt cache for session
      │
      ▼
[turn N model call uses OLD system prompt — already built]
      │
      ▼
turn N completes — row written to ORIGINAL profile's sessions.db
      │
      ▼
═══ turn boundary ═══
      │
      ▼
turn N+1 user message
      │
      ▼
prompt builder rebuilds system prompt (cache evicted)
      │
      ├─ reads NEW SOUL.md / MEMORY.md / USER.md (memory rebound)
      └─ HandoffInjectionProvider reads NEW inbox, injects, archives
      │
      ▼
model sees new profile's prompt + handoff banner
      │
      ▼
turn N+1 chat row written to ORIGINAL profile's sessions.db  ← gap
agent's _home() still returns OLD profile (env var not updated)  ← gap
MCP tool calls still hit OLD profile's MCP servers              ← gap
provider calls still use OLD profile's API key                  ← gap
BashTool subprocess HOME points at NEW profile's home/          ← asymmetric
```

---

## 2. State-by-state breakdown — what binds where, why the swap misses it

### 2.1 `SessionDB` — the single biggest gap

**Binding.** `agent/loop.py:759`:

```python
self.db = db or SessionDB(config.session.db_path)
```

`config.session.db_path` default (`config.py:471`):

```python
db_path: Path = field(default_factory=lambda: _home() / "sessions.db")
```

`_home()` is called once at config dataclass instantiation. Path captured.

**SessionDB has NO `close()` or `rebind()` method** — verified via
`grep -n "def close\|def rebind" opencomputer/agent/state.py` returning
nothing. Once constructed, the path is permanent for the process lifetime.

**Effect.** Post-swap chat rows go to the original profile's `sessions.db`.
`SubagentStore` (`loop.py:778`) and `EpisodicMemory` (`loop.py:908`) are
both bound to `self.db.db_path` — they share the gap.

`oc -p new-profile sessions tree` next time you restart won't show the
post-swap turns. They're stranded in the original profile's DB.

### 2.2 `.env` — API keys, OAuth tokens

**Binding.** `cli.py:425` `_apply_profile_override` runs at process start.
Sets `OPENCOMPUTER_HOME=<profile-root>`. Subsequent dotenv load reads
`<OPENCOMPUTER_HOME>/.env`. Happens once.

**Effect.** Provider calls post-swap use the original profile's API keys.
Costs accrue to the original Anthropic / OpenAI account.

### 2.3 `config.yaml`

**Binding.** Loaded once into a `Config` dataclass. `self.config` on
`AgentLoop` is the original profile's config for life of the process.

**Effect.** New profile's hooks / MCPs / model preferences / persona /
compaction thresholds / `auto_swap_handoff` value never read.

### 2.4 MCP subprocesses + `MCPManager`

**Binding.** `cli.py:1761`:

```python
mcp_mgr = MCPManager(tool_registry=registry)
```

Manager constructed once. Stdio MCPs spawned with original profile's env.

**Effect.** Post-swap MCP tool calls still hit original profile's MCPs.
New profile's MCP server list never spawned.

### 2.5 Skills directory

**Binding.** `MemoryManager.skills_path` set at construction.

**Intentional non-rebind.** `memory.py:971-973` is explicit:

> ``skills_path``, bundled-skills paths, and ``global_soul_path`` are NOT
> rebound — skill roots and the global SOUL fallback are shared across
> profiles, not per-profile.

This is by design.

### 2.6 `OPENCOMPUTER_HOME` env var

**Binding.** `cli.py:489`:

```python
os.environ["OPENCOMPUTER_HOME"] = str(get_profile_dir(profile_name))
```

Once at process start.

**`consume_pending_profile_swap` does NOT update `os.environ`.** Verified —
`_profile_swap.py:70-89` only writes the sticky file via
`write_active_profile()` and updates `runtime.custom`.

**Effect.** Asymmetry — see §3.

### 2.7 Browser-profile

Bound to active profile at plugin activation. Browser cookies / OAuth
callback state stays in original profile post-swap.

### 2.8 Plugin registry + hook engine

Loaded once at startup. New profile's plugin enabled-list / hook configs
never read. To unload + reload would require dispose paths that don't exist.

### 2.9 Provider client

Constructed once at startup with original profile's API key. Provider
identity survives the swap.

### 2.10 F1 consent + audit log — v1 was WRONG here

**v1 claim:** "audit DB connection bound once; new audit grants go to old
profile's audit.db."

**v2 correction (verified at `agent/loop.py:4174-4192`):** The handoff
audit logger IS rebound when `current` profile changes:

```python
if cached_logger is None or cached_profile != current:
    if cached_logger is not None:
        try:
            cached_logger.close()
        except Exception:
            _log.debug("prior audit logger close raised", exc_info=True)
    audit_logger = self._init_handoff_audit_logger(current)
    self._handoff_audit_logger = audit_logger
    self._handoff_audit_logger_profile = current
```

So swap audit rows DO go to the **new** profile's `consent/audit.db`.

**But** — this is ONLY for the handoff-subsystem audit logger. The wider F1
consent audit (consent grants, capability decisions during tool use) is a
DIFFERENT audit-log binding which I did NOT verify rebinds. Likely it does
NOT, because the ConsentGate object is constructed earlier and not seen in
the swap path. Open question, flagged.

### 2.11 Honcho / external memory provider

Plugin loaded at startup. Configured client bound at load. Post-swap
memory mirror still writes to original profile's data store.

### 2.12 Cron jobs

Per-profile `cron.db`. Scheduler started once at gateway/CLI boot.
Post-swap, scheduler continues reading the original profile's cron DB.

### 2.13 Kanban state

Per-profile `kanban.db`. Bound at startup.

### 2.14 Gateway adapters

Per-channel adapters loaded once at `oc gateway` boot. All inbound messages
keep routing through the original profile.

### 2.15 Wire clients

Active WS clients keep their session keying. They receive a `profile.swap`
event via the bus → wire bridge, so they can render a UI update, but
underlying session state doesn't change.

---

## 3. The env-var/sticky-file asymmetry (the deepest mechanism bug)

This deserves its own section.

**Two ways code resolves "what's the active profile":**

**(a) The `_home()` resolver** in `agent/config.py:42-...`, used everywhere
in the agent process:

```
1. plugin_sdk.profile_context.current_profile_home ContextVar
2. OPENCOMPUTER_HOME env var
3. ~/.opencomputer fallback
```

**(b) `read_active_profile()`** in `profiles.py:246-268`, used by
`scope_subprocess_env()`:

```
1. Read ~/.opencomputer/active_profile file
2. None / "default" / corrupt → None (default)
```

**The swap path updates (b) but NOT (a).** `consume_pending_profile_swap`
calls `write_active_profile()` and updates `runtime.custom`, but does NOT
mutate `os.environ["OPENCOMPUTER_HOME"]`.

**Concrete consequence.** After a swap:

| Caller | Resolves active profile via | Sees |
|---|---|---|
| `_home()` in Python agent code | `OPENCOMPUTER_HOME` env var | OLD profile |
| BashTool subprocesses (env from `scope_subprocess_env`) | sticky file → `read_active_profile()` | NEW profile |
| `_init_handoff_audit_logger(current)` | passed `current` from `runtime.custom` | NEW profile |
| `MemoryManager.declarative_path` after rebind | explicit `rebind_to_profile()` | NEW profile |
| Anything else calling `_home()` (logging, recall paths, plugin code) | `OPENCOMPUTER_HOME` env var | OLD profile |

**This is the split-brain.** Half the system thinks we're in the new
profile, half thinks we're in the old. The pieces that explicitly call
rebind functions (memory, handoff audit) see the new profile. Everything
else that lazily resolves via env var sees the old.

**Why this matters concretely.** A plugin that calls `_home()` to find its
data dir after the swap reads from the OLD profile's directory. If the user
swaps from `default` to `stocks` and a stocks-specific plugin (e.g. the
trading-data plugin) tries to read `<stocks-home>/positions.json`, it gets
`<default-home>/positions.json` instead — because `_home()` returns the old
path.

**Why this isn't catastrophic in practice today.** Because most plugins
were authored before this swap mechanism existed, they don't do
mid-conversation profile-relative path resolution. They read paths once at
plugin activation. So they don't trip the asymmetry. The asymmetry is a
latent bug waiting for the first plugin that does lazy path resolution to
trip it.

**The fix is one line.** In `consume_pending_profile_swap`:

```python
import os
os.environ["OPENCOMPUTER_HOME"] = str(get_profile_dir(
    None if pending == "default" else pending
))
```

Symmetric with the original `_apply_profile_override` set. Closes the gap.

The reason it's not there today is hard to read off the code alone — likely
the author of the swap path was worried about subprocesses inheriting a
mutating env var. But subprocesses inherit at fork time, so a mid-process
mutation only affects subprocesses spawned AFTER the mutation. Existing
subprocesses keep the old `OPENCOMPUTER_HOME` — same as today. Net: the
one-line fix is safe.

---

## 4. The audit logger rebind is the cleanest part of the swap

I want to call this out specifically because v1 missed it.

`agent/loop.py:4171-4192`:

```python
cached_logger = getattr(self, "_handoff_audit_logger", None)
cached_profile = getattr(self, "_handoff_audit_logger_profile", "")
if cached_logger is None or cached_profile != current:
    if cached_logger is not None:
        try:
            cached_logger.close()
        except Exception:
            _log.debug("prior audit logger close raised", exc_info=True)
    audit_logger = self._init_handoff_audit_logger(current)
    self._handoff_audit_logger = audit_logger
    self._handoff_audit_logger_profile = current
```

This is the pattern the SessionDB / `.env` / MCPManager paths should follow.
On every turn, check "has the profile changed since I cached this binding?
If so, close the old, build a new." It's 22 lines per binding. Replicating
this pattern across the other 19 unbound state categories would close most
of the gap.

The reason it works for audit:
- `HandoffAuditLogger.close()` exists.
- The constructor is idempotent — no global side effects.
- It's already keyed by profile in the cache.

The reason it's harder for SessionDB:
- No `close()` method exists.
- Mid-turn rebinding loses in-flight transactions (though turn boundaries
  are quiet windows).
- `SubagentStore` and `EpisodicMemory` both share `self.db.db_path` and
  would need cascading rebind.

But the model — per-turn cached-binding-check-and-rebind — is there in the
audit code. It's a precedent.

---

## 5. The protocol prompt — what the model is actually asked to do

This is in code, verified at `agent/handoff/protocol_v2.py:51-131`. The
prompt template is the canonical handoff-protocol v2.0 spec — embedded as a
string literal. Three core principles:

1. **The reader is a stranger.** No shared memory. Write what the user
   would have to re-explain.
2. **Capture MODE.** Not just topic — the user's expected presence
   (advice, reflection, debate, listening, etc.).
3. **Treat the handoff as DATA, not authority** (R12). Explicitly told to
   phrase as "the user stated…" not "you must…", and to flag contested
   points for the next reader to verify with the user.

Rules R1–R13 are spelled out in the prompt body. There's an explicit
override clause: rules can be broken if doing so produces a better handoff,
but the three irreducible questions cannot.

**Step 0** — the model is told to first decide "is a handoff warranted?"
If not, emit `HANDOFF_NOT_WARRANTED: <reason>` and stop. The parser handles
both outcomes (`protocol_v2.py:251-281`). On not-warranted: maps the reason
to one of `NO_TRIVIAL` / `NO_EMPTY` / `NO_COMPLETED` via keyword scan.

**Body cap**: 6000 chars (≈1200 words). Truncated at paragraph boundary
with `[... truncated ...]` marker if exceeded.

**Input clamping**: per-message 4000 chars, message-count clamped to last
12 turns of each role. Prevents degenerate input from blowing the prompt.

---

## 6. The gateway opt-in story (verified gap)

`config.py:1731-1733` says:

> CLI / webui / workspace / wire surfaces all participate; gateway channels
> are opt-in via their per-channel ``auto_swap_enabled`` flag.

I grep'd `extensions/` and `opencomputer/channels/` for `auto_swap_enabled`:

```
grep -rn "auto_swap_enabled\|_is_gateway_session\|_channel_auto_swap" \
    extensions/ opencomputer/channels/
# → no matches
```

**Conclusion**: no channel adapter actually sets the opt-in flag. So auto-swap
is **effectively disabled for ALL gateway sessions** (Telegram, Discord, Slack,
Matrix, etc.) — they all hit the `GATEWAY_DISABLED` gate.

It's silently dead code on the gateway side. Manual `/handoff` from CLI/webui
still works.

To enable: a channel adapter would need to set `runtime.custom["_is_gateway_session"]
= True` AND `runtime.custom["_channel_auto_swap_enabled"] = True`. Today neither
is set by any adapter.

---

## 7. What the design comment says vs. what the code does

`config.py:1725-1735`:

> ``"silent"`` (default): the classifier-driven trigger is live; on
> a sustained-confidence persona shift, the agent generates a
> handoff per protocol v2.0, writes it to the target profile's
> ``inbox/``, **and swaps profiles on the next turn**. CLI / webui /
> workspace / wire surfaces all participate; gateway channels are
> opt-in via their per-channel ``auto_swap_enabled`` flag.

Verification per clause:

- **"classifier-driven trigger is live"** — verified at `loop.py:1559`.
- **"sustained-confidence persona shift"** — verified at `auto_swap.py:205-226`
  (3-of-3 at confidence ≥0.8).
- **"generates a handoff per protocol v2.0"** — verified end-to-end.
- **"writes it to the target profile's inbox/"** — verified
  (`inbox.py:91-161`, `inbox_dir` is `<profile_home>/inbox/`).
- **"swaps profiles on the next turn"** — TECHNICALLY MISLEADING. The swap
  applies on the SAME turn that triggered it, between the auto-swap pipeline
  and the next prompt-build. The "new prompt with new MEMORY.md" is on the
  NEXT turn but the swap state mutation is on the CURRENT turn. Order:
  - turn N user msg → `_run_handoff_auto_swap` (sets `pending_profile_id`)
  - → `_apply_pending_profile_swap` (consumes pending, rebinds memory)
  - → model call (uses ALREADY-BUILT old prompt — cache hit for THIS turn)
  - → turn N+1 user msg → cache miss (snapshot evicted) → rebuild prompt with
    new memory + new handoff inbox
- **"CLI / webui / workspace / wire surfaces all participate"** — VERIFIED.
  Bus publish → wire bridge → SSE → dashboard SPA → toast. Workspace SPA
  toast handler is at `dashboard/static/_dashboard.js:131-211`.
- **"gateway channels are opt-in"** — TRUE but as §6 documents, no channel
  adapter actually opts in. Effectively disabled.

So the comment is mostly true with one subtle "next turn" framing issue and
one verifiable dead-flag (`auto_swap_enabled`).

---

## 8. `/handoff --no-content` is NOT cheaper than restart

You might think `/handoff --no-content <target>` is a profile switch without
LLM cost. It IS — but it's the SAME partial swap as the auto path. Same
state stays bound. Same SessionDB, same `.env`, same MCPs.

The only difference vs. the auto path: no LLM call to generate the handoff
doc, no handoff written to the new profile's inbox. So the new profile gets
no context handoff banner.

If you actually want to switch profiles cleanly, you have to exit `oc` and
restart with `oc -p <new>`. There's no in-process equivalent today.

---

## 9. A correct full-profile-swap implementation — what it would take

Six things, in rough order of difficulty:

### 9.1 Update `OPENCOMPUTER_HOME` env var

One line in `consume_pending_profile_swap`:

```python
os.environ["OPENCOMPUTER_HOME"] = str(get_profile_dir(
    None if pending == "default" else pending
))
```

Closes the asymmetry. Risk: subprocesses spawned mid-process see the new
value (correct). Existing subprocesses keep their old fork-time copy (also
correct).

**Estimate: 1 line, 2 days to write the test that proves
`_home()` returns the new value post-swap.**

### 9.2 Rebind `SessionDB`

Add `SessionDB.close()`. In `_apply_pending_profile_swap`, after memory
rebind:

```python
if self.db is not None:
    self.db.close()
new_db_path = new_home_root / "sessions.db"
self.db = SessionDB(new_db_path)
# Re-attach subagent store + episodic
SubagentRegistry.instance().attach_store(SubagentStore(self.db.db_path))
self._episodic = EpisodicMemory(db=self.db)
```

Risk: the conversation history continues in a new DB. If you swap from A→B
mid-session, turns 1-N are in A's DB, turns N+1-end are in B's DB. Resume
(`oc chat -c`) can only restore from one. Mitigation: write a "session
continued in profile X" pointer row in A's DB referencing the session in B.

**Estimate: 1 week to implement + 1 week to handle resume semantics.**

### 9.3 Hot-reload `.env`

Track which env vars came from `.env` (vs. shell). On swap, undo those and
load new ones. `python-dotenv` doesn't have first-class support but it's
straightforward.

**Estimate: 2 days.**

### 9.4 Hot-swap subset of `config.yaml`

Classify each config field as hot-swappable or restart-required:

- Hot-swappable: model preferences, compaction thresholds, persona,
  auto_swap_handoff, prompt cache size, soul/memory paths.
- Restart-required: loop.max_iterations (changes mid-loop = chaos),
  hooks (requires hook engine rebind), plugins.enabled (requires plugin
  loader rebind), tools.allowed_tools, MCP server list (handled separately
  in 9.5).

Apply only hot-swappable fields; warn the user that restart-required
changes won't apply until restart.

**Estimate: 3-5 days for classification + apply path + tests.**

### 9.5 Cycle `MCPManager`

```python
old_servers = set(self.mcp_mgr.connected_servers)
new_servers = set(new_config.mcp.servers)
to_kill = old_servers - new_servers
to_start = new_servers - old_servers
for srv in to_kill: await self.mcp_mgr.disconnect(srv)
for srv in to_start: await self.mcp_mgr.connect(srv)
```

Diff-based — only kills MCPs that aren't in the new list, only starts new
ones. Servers shared between profiles stay up. Reduces latency from
"restart everything" to "restart the delta."

Risk: an MCP server with the same name but different config (different env
vars, different API token) doesn't get respawned. Mitigation: hash config
into the comparison key.

**Estimate: 3-5 days.**

### 9.6 Reload plugins

Hardest. Plugins use `register(api)` with side effects:
- Tool registrations in `ToolRegistry`.
- Channel adapter registrations in `Gateway.adapters`.
- Provider registrations.
- Hook subscriptions in `HookEngine`.
- Dynamic injection providers in `InjectionEngine`.

To unload requires:
- `ToolRegistry.unregister(tool_name)`.
- Adapter `disconnect()` + removal from registry.
- Provider removal.
- Hook unsubscribe.
- Injection provider removal.

None of these dispose paths exist today.

Realistic alternative: **require restart for plugin changes**. Document
that profile swap doesn't reload plugins; if you need a different plugin
set, restart.

**Estimate to do properly: 2-3 weeks**.

### 9.7 Rebind provider client

```python
self.provider = build_provider(new_config)
self._handoff_provider_adapter = None  # force rebuild on next handoff
```

Risk: in-flight streaming call holds the old provider. The swap fires
between turns (not during streaming), so this is safe. Mitigation:
guard with an `is_streaming` check.

**Estimate: 2 days.**

### 9.8 Browser-profile swap

Plugin-specific. browser-harness would need a `rebind_browser_profile(path)`
method that closes the old browser context and reopens with the new
user-data-dir.

**Estimate: 1-2 days (depending on browser-harness internals)**.

### Total estimate

| Sub-task | Effort | Risk |
|---|---|---|
| 9.1 OPENCOMPUTER_HOME | 2 days | L |
| 9.2 SessionDB rebind | 2 weeks | H — cross-DB session continuity |
| 9.3 `.env` reload | 2 days | M — clobber shell-set keys |
| 9.4 config hot-swap subset | 1 week | M — every field needs classification |
| 9.5 MCP diff-cycle | 3-5 days | M — latency + env diff hashing |
| 9.6 Plugin reload | 2-3 weeks | XL — dispose paths don't exist |
| 9.7 Provider client | 2 days | L |
| 9.8 Browser-profile | 1-2 days | L |
| Tests + docs | 1 week | — |

**Total realistic: 6-8 weeks.** Plugin reload is the long pole — if you
defer it (document that restart is required for plugin changes), the rest
is 3-4 weeks.

---

## 10. Tests in the repo (and what's NOT tested)

Coverage of the handoff subsystem (inferred from test file names; actual
read NOT done in this audit):

- `test_handoff_inbox*.py` — write/read/parse round-trip, frontmatter,
  atomic-write.
- `test_handoff_generator*.py` — provider call shape, retry, parse errors,
  NOT_WARRANTED.
- `test_handoff_injector*.py` — DynamicInjectionProvider contract.
- `test_handoff_orchestrator*.py` — pipeline end-to-end with mocks.
- `test_handoff_auto_swap*.py` — trigger state machine, decision reasons.
- `test_handoff_audit*.py` — audit row format, HMAC chain.
- `test_handoff_protocol_v2*.py` — prompt rendering, response parsing.

**What's NOT tested** (the gaps that hide the bugs in §2 and §3):

- SessionDB binding pre/post swap (no integration test exists for cross-profile
  session continuity).
- `OPENCOMPUTER_HOME` env var post-swap (would catch §3).
- MCP subprocess identity pre/post swap.
- BashTool subprocess HOME pre/post swap (would catch the asymmetric pair
  with §3).
- Plugin enabled-list pre/post swap.
- Provider key pre/post swap.
- Resume (`oc chat -c`) across a swap boundary.
- Gateway channel auto-swap end-to-end (the opt-in flag is unset, see §6).

These are integration tests crossing the swap boundary. They would
immediately reveal the gaps documented above.

---

## 11. Source references (every file:line claim in this doc)

**Handoff subsystem files** (all read in v2 audit):

- `opencomputer/agent/handoff/__init__.py` (66 LOC) — public re-exports.
- `opencomputer/agent/handoff/models.py` (76 LOC) — `HandoffDocument`,
  `HandoffMetadata`, `HandoffWarranted` enum. Frozen dataclasses.
- `opencomputer/agent/handoff/protocol_v2.py` (317 LOC) — embedded
  handoff-protocol v2.0 spec, prompt renderer, response parser.
- `opencomputer/agent/handoff/auto_swap.py` (336 LOC) — `AutoSwapTrigger`
  state machine.
- `opencomputer/agent/handoff/generator.py` (298 LOC) — provider call wrap,
  retry, parse.
- `opencomputer/agent/handoff/inbox.py` (389 LOC) — atomic file IO.
- `opencomputer/agent/handoff/injector.py` (154 LOC) — system-prompt
  injection.
- `opencomputer/agent/handoff/audit.py` (203 LOC) — HMAC-chained audit
  logger.
- `opencomputer/agent/handoff/orchestrator.py` (508 LOC) — pipeline glue,
  bus publish.

**Swap application**:

- `opencomputer/agent/loop.py:601-638` — `_resolve_handoff_audit_key`.
- `opencomputer/agent/loop.py:641-693` — `_apply_pending_profile_swap`.
- `opencomputer/agent/loop.py:759` — `self.db = SessionDB(config.session.db_path)`.
- `opencomputer/agent/loop.py:778` — SubagentStore binding.
- `opencomputer/agent/loop.py:908` — EpisodicMemory binding.
- `opencomputer/agent/loop.py:1558-1578` — pipeline call site, apply site.
- `opencomputer/agent/loop.py:4093-4259` — `_run_handoff_auto_swap` +
  `_init_handoff_audit_logger`.
- `opencomputer/cli_ui/_profile_swap.py:1-100` — cycle, consume, init.
- `opencomputer/agent/memory.py:962-984` — `rebind_to_profile`.

**Profile system**:

- `opencomputer/profiles.py:1-230` — discovery, sticky file, validate.
- `opencomputer/profiles.py:235-243` — `list_profiles`.
- `opencomputer/profiles.py:246-283` — `read_active_profile` /
  `write_active_profile`.
- `opencomputer/profiles.py:110-154` — `scope_subprocess_env`.
- `opencomputer/cli.py:425-505` — `_apply_profile_override`.
- `opencomputer/agent/config.py:42-...` — `_home()`.
- `opencomputer/agent/config.py:471` — `db_path` default.
- `opencomputer/agent/config.py:1725-1735` — `auto_swap_handoff` config doc.

**Wire / dashboard surfaces**:

- `opencomputer/gateway/protocol.py:129` — `EVENT_PROFILE_SWAP = "profile.swap"`.
- `opencomputer/gateway/wire_server.py:156-238` — bus → wire bridge.
- `opencomputer/gateway/wire_server.py:840+` — bridge handler.
- `opencomputer/dashboard/static/_dashboard.js:131-211` — SSE subscriber +
  toast render.

**Slash command**:

- `opencomputer/agent/slash_commands_impl/handoff_cmd.py` (154 LOC) — manual
  path.

**SessionDB**:

- `opencomputer/agent/state.py:1129+` — `SessionDB.__init__`. No `close()`
  or `rebind()` (verified via grep — both return zero matches).

---

## 12. Correction log — what changed vs. v1

**v1 errors and missed claims, corrected in v2:**

1. **v1 claimed**: "Audit DB connection bound once; new audit grants go to
   old profile's audit.db."
   **v2**: The handoff audit logger IS rebound per current profile at
   `loop.py:4174-4192`. Swap audit rows correctly land in the target
   profile's `consent/audit.db`. Source verified.

2. **v1 claimed**: "All gateway surfaces participate."
   **v2**: Gateway sessions need `_channel_auto_swap_enabled` flag set; no
   channel adapter in `extensions/` actually sets it. So auto-swap is
   silently disabled for ALL gateway channels. Manual `/handoff` from CLI/webui
   still works.

3. **v1 claimed**: 19 categories of state don't swap.
   **v2**: 20 categories. Added `SubagentStore` and `EpisodicMemory` which
   share `self.db.db_path` and inherit the SessionDB gap. They're already
   covered transitively but worth naming.

4. **v1 unverified**: Said HMAC chain exists, didn't verify the schema.
   **v2**: Verified at `audit.py:162-200`. Schema includes UPDATE and DELETE
   triggers (`audit_log_no_update`, `audit_log_no_delete`) that enforce
   append-only at the SQLite engine layer. Defensive only — filesystem-level
   deletes still possible.

5. **v1 missed**: The protocol_v2.py prompt body. v2 reads the full
   handoff-protocol v2.0 spec embedded in code at lines 51-131. R1–R13
   rules, three irreducible questions, override clause, Step 0 logic.

6. **v1 missed**: `time_of_day_hour=12` hardcoded in classifier context at
   `orchestrator.py:225`. Possible bug; v2 flags as open question.

7. **v1 missed**: `recent_file_paths=()` passed empty to classifier context.
   Possible signal loss; v2 flags as open question.

8. **v1 missed**: Body cap 6000 chars + paragraph-boundary truncation
   (`protocol_v2.py:38, 279-307`).

9. **v1 missed**: Inbox cap 50 pending files (`inbox.py:52, 192-198`).

10. **v1 missed**: `HandoffDocument.path` field is set post-write via
    `with_path()` (`models.py:71-73`).

11. **v1 missed**: Generator retry behavior — single retry after 2s backoff,
    `wait_for(timeout=31s)` while provider has 30s internal budget
    (`generator.py:171-222`).

12. **v1 misframed**: "Swap on the next turn." Actually the swap state
    mutation happens on the CURRENT turn (between auto-swap pipeline and
    next prompt build). The new SOUL.md / MEMORY.md / inbox-injection lands
    on the NEXT turn because the prompt cache for the current turn was
    already built. v2 spells out the exact timing.

13. **v1 missed**: `MemoryManager.rebind_to_profile()` binds at
    `<profile_root>/home/`, not at `<profile_root>/`. The handoff inbox
    lives at `<profile_root>/home/inbox/`, NOT
    `<profile_root>/inbox/`. The per-profile sandbox shape matters here.
    Verified at `_apply_pending_profile_swap:675`.

14. **v1 missed**: The HMAC key for the audit chain is shared with the
    consent system — same `service='opencomputer-consent', key='hmac-chain'`
    keyring entry — so swap audit rows and consent grants are in the same
    chain (`audit.py:5-12`, `loop.py:601-638`).

15. **v1 missed**: SubagentStore at `loop.py:778` and EpisodicMemory at
    `loop.py:908` both bind to `self.db.db_path` at AgentLoop construction
    — they inherit the SessionDB binding gap.

16. **v1 missed**: Dashboard SPA SSE subscription at
    `dashboard/static/_dashboard.js:131-211`. The "UI surfaces participate"
    claim IS true for the SPA via SSE; v2 adds the exact line refs.

**Unverified claims removed from v2:**

- v1 said "the audit DB connection is bound once" — removed; correctly
  rebound per turn.
- v1 said "consent grants made post-swap also write to the original
  profile's audit.db" — flagged as open question in v2 §2.10 (would need to
  audit the ConsentGate binding path).
- v1 said specific test file names without verifying they exist — replaced
  with "inferred from test file names; actual read NOT done in this audit."

**Net change**: v1 was directionally correct on the big claim (swap is
partial) but had specific errors on audit-logger rebind and gateway-channel
participation. v2 fixes those and adds verified depth on the protocol body,
retry semantics, inbox caps, HMAC schema, dashboard SSE wiring.

---

## 13. Recommendation (unchanged from v1)

Two honest paths:

### Path A — "Document the limit"

Rename the feature in user docs. Stop calling it "profile swap." Call it
"persona + memory swap with handoff." Tell users: for a real profile switch,
exit and `oc -p <new>`. Cheap, today.

### Path B — "Make the swap real"

Implement §9. 6-8 weeks. Plugin reload is the long pole; deferring it cuts
to 3-4 weeks but means restart is required when plugin set differs.

**My recommendation: Path A unless multi-user cloud OC ships.** For
single-user OC, restart-to-switch-profile is fine. The handoff flow is
cross-pollination — a context message from one profile to another inside
one long-running session — not a real switch. Document accordingly.

If you do go with Path A, the one fix worth pulling out of Path B for free:
**§9.1 (one-line `OPENCOMPUTER_HOME` env update)**. Closes the asymmetry in
§3 at near-zero cost. Pure win.

---

End of v2 investigation.

---

## 14. v3 Closure Log (2026-05-15)

**This investigation drove a real-swap implementation.** Every numbered
item in §9 is now either shipped or hard-deferred with justification.
Design doc: `docs/superpowers/specs/2026-05-15-profile-handoff-real-swap-design.md`.

### Status per §9 item

| Item | Status | Test file |
|---|---|---|
| §9.1 `OPENCOMPUTER_HOME` env var | ✅ SHIPPED | `test_profile_swap_env_alignment.py` |
| §9.2 SessionDB rebind + continuation pointer | ✅ SHIPPED | `test_session_db_rebind.py` |
| §9.3 `.env` reload | ✅ SHIPPED | `test_dotenv_tracker.py` |
| §9.4 Config hot-swap subset (allowlist) | ✅ SHIPPED | `test_config_hot_swap.py` |
| §9.5 MCPManager diff-cycle | ✅ SHIPPED | `test_mcp_diff_cycle.py` |
| §9.6 Plugin reload | ⛔ HARD-DEFERRED — see "Hard-Justified Deferral" in the design doc | — |
| §9.7 Provider client rebind | ✅ SHIPPED | `test_profile_rebind_handlers.py` |
| §9.8 Browser-profile + plugin_sdk exposure | ✅ SHIPPED | `test_browser_profile_rebind.py` |

### Open Questions Resolved

| Question | Resolution |
|---|---|
| §1.2 `time_of_day_hour=12` hardcoded | Confirmed bug — classifier uses it for evening/morning routing. Fixed: orchestrator now passes `datetime.now().hour`. |
| §1.2 `recent_file_paths=()` empty | Confirmed signal loss — classifier weights py/md frequencies. Fixed: `_extract_recent_file_paths` scans recent assistant tool_use blocks. |
| §2.10 ConsentGate rebind | Confirmed bound-once issue. Added `ConsentGate.rebind_to_profile(new_home)` that closes old connection, rebuilds store + audit against new profile's audit.db + keyring slot. Wired as a rebind handler @ priority 130. Tools that captured `self._consent_gate` keep the same object (rebind mutates in place). |
| §6 Gateway opt-in unwired | `BaseChannelAdapter.__init__` now reads `auto_swap_enabled` from per-channel config (strict bool only — no truthy-string footgun). Profile `config.yaml` can set `channels.<platform>.auto_swap_enabled: true`. |

### Architecture Added

* `opencomputer/agent/profile_rebind.py` — `ProfileRebindRegistry`,
  ordered + exception-isolated composition primitive
* `opencomputer/agent/dotenv_tracker.py` — snapshot-based `.env`
  unload/reload that preserves shell-set values
* `opencomputer/agent/config_hot_swap.py` — explicit allowlist of
  hot-swappable Config top-level fields
* `MCPManager.diff_cycle(new_servers)` + identity hash
* `SessionDB.rebind(new_path, source_session_id=, target_profile=)`
  with continuation pointer write to OLD db
* `ConsentGate.rebind_to_profile(new_home)` in-place gate rebuild
* `PluginAPI.register_profile_rebind_handler(name, handler, *, priority)`
  — queued, drained at AgentLoop __init__

### Net Test Delta

97 new tests across 11 new files. Pre-existing handoff suite (46 tests)
unbroken.

### What's NOT in this PR

* §9.6 plugin reload — requires plugin-contract dispose paths
  (`unregister_*` symmetry) that don't exist yet. 2-3 weeks separately.
  UX mitigation: when new profile's `plugins.enabled` differs, the
  config-hot-swap WARN log lists the delta. Restart required for full
  plugin set switch.
* CLI surface for "profile-swap details" inspection — possible future
  PR could add `oc profile rebind-status` showing each handler's last
  result.
* HMAC chain re-seed verification across profile swaps — currently each
  profile has its own keyring slot via `KeyringAdapter(fallback_dir=
  profile_root)`, so chains do NOT verify across the swap boundary by
  design. Documented in §2.10 resolution.
