# Profile-as-Agent — parallel multi-profile gateway routing

**Status:** Design approved (2026-04-30) — pending implementation plan

**Author:** archit-2 brainstorm session

**Reference notes:** `docs/superpowers/specs/2026-04-28-openclaw-tier1-port-design.md`,
brainstorm context in this session's transcript ("openclaw-notes.md" extraction
+ gap analysis + Option A selection).

---

## 1. Why

OpenComputer's gateway today routes every inbound message to **one**
`AgentLoop`, instantiated against **one** profile picked at process start.
That mirrors most personal-agent frameworks (Hermes too) and works fine
for "one profile per terminal."

OpenClaw's headline architectural advantage is that a single gateway daemon
runs **multiple agents simultaneously**, and inbound messages route to the
right one based on a declarative binding table. The Telegram chat with the
user's stock analysis agent is not the same agent as the Telegram chat
with the user's coding agent — different system prompt, different memory,
different tools, different model.

OpenComputer already has *almost everything* OpenClaw calls an "agent" —
under the name **profile**. Profiles already own:
- system prompt (`prompts/`, `MEMORY.md`, `SOUL.md`)
- tools (`plugins.enabled` filter)
- memory (per-profile `sessions.db` + `MEMORY.md`)
- model config (per-profile `config.yaml` `model.*`)
- skills (`skills/`)
- subagent templates (`agents/*.md`)

The single missing piece is **gateway-level per-message profile selection**:
today only one profile is "live" at a time per process. This design lifts
that constraint without inventing a parallel "Agent" abstraction.

**Framing decision (locked, brainstorm 2026-04-30):**
agent ≡ profile. We do not introduce a new `Agent` dataclass distinct from
profile. The gateway becomes profile-aware per inbound message.

**Use-case decision (locked):** multiple concurrently-active profiles
(2+ at any given moment), with bindings selecting per-chat — e.g.
"Telegram chat A → coding profile, Telegram chat B → stock profile,
everything else → default."

**Concurrency decision (locked):** parallel — two chats from two profiles
process simultaneously in independent AgentLoops. Not swap-on-demand.

## 2. What

A new gateway-level routing layer:

1. **`AgentRouter`** — caches `dict[profile_id, AgentLoop]`. Builds an
   AgentLoop for a profile lazily on first inbound, keeps it warm.
2. **`BindingResolver`** — reads `bindings.yaml`, returns `profile_id`
   for a given `MessageEvent`.
3. **`ProfileContext`** (`contextvars.ContextVar`) — `current_profile_home`
   is set per dispatch task. `_home()` consults it before falling back to
   `OPENCOMPUTER_HOME` env var or `~/.opencomputer/default`.
4. **`bindings.yaml`** — declarative routing table at
   `~/.opencomputer/bindings.yaml` (gateway-level, not profile-level).
5. **`oc bindings`** CLI — `add / list / remove / show`, flock'd YAML
   writer (closes the latent profile.yaml flock tech debt at the same time).

The dispatcher's contract becomes:

```
inbound MessageEvent
  → BindingResolver.resolve(event)            # → profile_id (str)
  → AgentRouter.get_or_load(profile_id)       # → AgentLoop (cached)
  → with set_profile(profile_home):           # ContextVar scoped
       await loop.run_conversation(...)
```

True parallelism: each AgentLoop has its own `MemoryManager`,
`SessionDB`, `Config`, and tool-allowlist filter. Each task gets its own
ContextVar value (Python's `contextvars` makes this safe across
`asyncio.Task` boundaries by design).

## 3. Architecture

```
                        Telegram / Discord / Slack / …
                                     │
                                     ▼
                              ┌──────────────┐
                              │   Gateway    │  ← constructs router + resolver
                              └──────┬───────┘     once at boot
                                     │
                                     ▼
                          ┌────────────────────┐
                          │     Dispatch       │
                          │  (handle_message)  │
                          └────────┬───────────┘
                                   │
              1. resolve binding → profile_id
              2. agent_router.get_or_load(profile_id)
              3. set ContextVar(profile_home)
              4. await loop.run_conversation(...)
                                   │
            ┌──────────────────────┼──────────────────────┐
            ▼                      ▼                      ▼
     ┌─────────────┐        ┌─────────────┐        ┌─────────────┐
     │ AgentLoop   │        │ AgentLoop   │        │ AgentLoop   │
     │ (default)   │        │ (coding)    │        │ (stock)     │
     │             │        │             │        │             │
     │ MemoryMgr   │        │ MemoryMgr   │        │ MemoryMgr   │
     │ SessionDB   │        │ SessionDB   │        │ SessionDB   │
     │ Config      │        │ Config      │        │ Config      │
     │ Tool filter │        │ Tool filter │        │ Tool filter │
     └─────────────┘        └─────────────┘        └─────────────┘
```

## 4. Components

### New

| Component | Path | Responsibility |
|---|---|---|
| `AgentRouter` | `opencomputer/gateway/agent_router.py` | Cache `{profile_id: AgentLoop}`. `async get_or_load(profile_id) -> AgentLoop`. Per-profile-id construction lock so two simultaneous first-inbounds don't double-build. Tracks "broken" profiles + retries on next inbound. |
| `BindingResolver` | `opencomputer/gateway/binding_resolver.py` | Load `bindings.yaml`. `resolve(MessageEvent) -> str` returns matching profile_id by precedence. Hot-reload via file mtime check (best-effort, optional). |
| `ProfileContext` | `plugin_sdk/profile_context.py` | `current_profile_home: ContextVar[Path | None]`. `set_profile(home: Path) -> ContextManager` returning a token-resetting CM. |
| `BindingsConfig` | `opencomputer/agent/bindings_config.py` | Frozen dataclass + YAML loader/saver. `Binding(match, profile_id, priority)`. |
| `oc bindings` CLI | `opencomputer/cli_bindings.py` | Typer subgroup. flock'd writes to `bindings.yaml`. |

### Changed

| File | Change |
|---|---|
| `opencomputer/agent/config.py` | `_home()` consults `ProfileContext.current_profile_home` first. All other call sites untouched. |
| `opencomputer/gateway/dispatch.py` | `_do_dispatch` resolves profile, gets loop, sets ContextVar, runs. Per-chat lock keys on `(profile_id, chat_id)` tuple. |
| `opencomputer/gateway/server.py` | `Gateway.__init__` constructs `AgentRouter` + `BindingResolver`. Backwards-compat: if caller passes a single `loop`, that becomes the synthesized "default" entry in the router. |
| `opencomputer/cli.py` | `chat` / `gateway` / `wire` entry points use the router pattern. Single-profile CLI users see no behavior change. |
| `opencomputer/tools/delegate.py` | `set_factory` and `_templates` move from class attributes to instance fields (latent fix; multi-profile makes the bug observable). |
| `opencomputer/plugins/registry.py` | Plugin modules loaded once at boot (union of all profiles' enabled lists). Each AgentLoop gets a per-profile filter. |

### Untouched

- `MessageEvent` shape, `BaseChannelAdapter` contract — no plugin SDK churn.
- All 30 channel adapters in `extensions/` — they hand `MessageEvent` to `Dispatch`; profile-blind.
- Tool contract, provider contract, hook engine.
- `agent_templates.py` — `_active_profile_root()` already calls `_home()`,
  so it becomes profile-aware automatically.

## 5. Data flow

```
Telegram inbound: chat_id=12345, text="..."
   │
   ▼
TelegramAdapter._on_message → MessageEvent(platform=telegram, chat_id=12345, ...)
   │
   ▼
Dispatch.handle_message(event)
   │
   ├─ photo-burst merging  (unchanged)
   ├─ channel-directory record  (unchanged)
   │
   ▼
Dispatch._do_dispatch(event, session_id):
   1. profile_id = binding_resolver.resolve(event)            # → "coding"
   2. profile_home = ~/.opencomputer/coding
   3. lock = locks.setdefault((profile_id, session_id), Lock())
   4. async with lock:
      5. with set_profile(profile_home):                       # ContextVar set
         6. loop = await agent_router.get_or_load("coding")    # cached / built
         7. await loop.run_conversation(user_message, session_id, ...)
              # _home() returns coding's home throughout the call
              # MemoryManager reads coding's MEMORY.md
              # SessionDB reads coding's sessions.db
              # Tool registry filtered to coding's enabled plugins
   8. return final_message.content
```

A concurrent inbound for `chat_id=67890` resolving to `"stock"` runs in
parallel: different lock key, different AgentLoop, different ContextVar
value in its own `asyncio.Task`.

## 6. `bindings.yaml` schema

Location: `~/.opencomputer/bindings.yaml` (root, *not* inside a profile).

```yaml
default_profile: default
bindings:
  - match: { platform: telegram, chat_id: "12345" }
    profile: coding
    priority: 100
  - match: { platform: telegram, chat_id: "67890" }
    profile: stock
    priority: 100
  - match: { platform: telegram }       # platform-wide fallback
    profile: personal
    priority: 10
```

Match precedence (specificity, ties broken by `priority` descending):
1. `peer_id` match
2. `chat_id` match
3. `group_id` match
4. `account_id` match
5. `platform` match
6. fall through to `default_profile`

Match field semantics:
- All match fields are optional. A binding with empty `match: {}` matches
  every event (catch-all).
- Match values are exact-string. (No regex / glob in v1; can add later.)
- Multiple fields in one `match` are AND-ed.

## 7. Error handling

| Failure | Behavior |
|---|---|
| Binding references non-existent profile | log ERROR, fall back to `default_profile`. Continue. |
| Profile dir doesn't exist | log ERROR, fall back to `default_profile`. (No auto-create — that's a CLI action.) |
| Malformed `bindings.yaml` | log ERROR at gateway boot, treat as empty (only `default_profile` catches inbound). Gateway boots. |
| AgentLoop construction fails for profile X | log ERROR, fall back to `default_profile` for *that turn*, mark X as "broken" in router, retry construction on next inbound. |
| ContextVar leak between tasks | not possible by design (`contextvars.ContextVar` is per-Task). Test covers explicitly. |
| Two simultaneous first-inbounds for same profile | `AgentRouter.get_or_load` uses an `asyncio.Lock` per profile_id during construction → second caller awaits, then returns the cached loop. |

## 8. Backwards compatibility

The single-profile path stays the default. A user with no `bindings.yaml`:

- Gateway boots. Resolver loads empty config. `default_profile` = `"default"`
  (or whatever env var / `--profile` flag selected).
- Every inbound matches the catch-all → routes to default profile.
- AgentRouter has one entry. ContextVar is set per dispatch but matches
  what `_home()` would have returned anyway.

Single-profile users observe **zero behavior difference**. All 885 existing
tests pass unmodified (verified by Phase 1 — see Phasing).

## 9. Phasing

Four independently-shippable PRs.

**Phase 1 — ContextVar plumbing.** Adds `ProfileContext`, refactors
`_home()` to consult ContextVar first. No behavior change. ~50 LOC + tests.

**Phase 2 — `AgentRouter` (lazy multi-profile).** Adds the router; gateway
constructs it; dispatcher uses it but always for `"default"` profile.
No behavior change. ~200 LOC + tests.

**Phase 3 — `BindingResolver` + actual routing.** Adds the resolver and
`bindings.yaml` schema. Dispatcher wires through. Per-`(profile, chat)`
lock keys. **First user-visible change** — but only if `bindings.yaml`
exists. Without one, default profile catches everything. ~300 LOC + tests.

**Phase 4 — CLI + docs.** `oc bindings add/list/remove/show`. README
"Multi-Profile" section. CHANGELOG. ~200 LOC + tests.

Phase ordering preserves "ship one thing at a time, never break main."

## 10. Testing

Three new test files plus extensions:

- `tests/test_phase_profile_context.py` — ContextVar isolation across tasks,
  fallback chain (ContextVar → env var → default), reset semantics.
- `tests/test_agent_router.py` — lazy load, cache hit, double-load lock,
  broken-profile recovery on retry.
- `tests/test_binding_resolver.py` — match precedence, priority ties,
  default fallback, malformed YAML, hot-reload (if implemented).
- `tests/test_dispatch_multiprofile.py` — **the critical one**: two
  AgentLoops for two profiles run *truly in parallel* (one with a sleep,
  the other completes first), each ContextVar isolated, MemoryManager
  isolated (write in A, read from B → not found), tool allowlists differ.
- `tests/test_phase6a.py` extension — SDK boundary check covers
  `plugin_sdk/profile_context.py`.

All 885 existing tests stay green at every phase.

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| `_home()` callers we missed | Phase 1 includes a grep audit + a test that exercises every module-import-time path under a non-default ContextVar. |
| `PluginRegistry` singleton fights us | Phase 2 explicitly tests two AgentLoops with disjoint `enabled_plugins`. Filter at AgentLoop construction, not registry-wide. |
| `DelegateTool` class-level state collides between profiles | Phase 2 moves factory + templates from class to instance. Latent bug; this exposes it. |
| Honcho memory overlay assumes one profile | Detect at AgentRouter construction; warn once if any profile has Honcho enabled, document per-profile-keyed semantics in README. |
| Schema drift in `bindings.yaml` | Strict load: reject unknown top-level keys. Forward-only schema migrations. |
| Hot-reload race conditions | Optional in v1; if implemented, use mtime check + read-locked reload. Fall back to `oc gateway` restart for first cut. |
| Provider state shared across profiles | Provider classes are stateless beyond config; each AgentLoop constructs its own. Verified by audit. |
| Memory leak: AgentLoops accumulate forever | v1: no eviction. Profile count is small (3-5). v2 if needed: LRU eviction with idle threshold. |

## 12. Out of scope

- Sub-process per profile (Option C from brainstorm). Hard isolation
  if security model ever requires it; not now.
- Mid-flight profile switch within one chat. (User says `/use coding`
  mid-conversation.) Possible later via slash command; not now.
- Cross-profile shared memory ("personal events visible from coding").
  Explicitly *not* a goal — we picked Option A for hard walls.
- Cron / scheduled inbounds routing through bindings.
- Per-binding overrides for system prompt (use a profile instead).
- Web UI for managing bindings.

## 13. Migration / rollback

Each phase is revertable in isolation. No on-disk migrations. `bindings.yaml`
is purely additive. ContextVar default behavior matches today's `_home()`
behavior bit-for-bit. If Phase 3 ships and a regression appears, revert
Phase 3 → resolver dormant → all messages go to default profile (today's
behavior). No data loss possible.

---

## Approval

Brainstorm decisions locked 2026-04-30 with the user:
- Framing: profile-as-agent (not new Agent abstraction)
- Use case: multiple concurrent profiles, per-chat routing
- Concurrency: true parallel
- `bindings.yaml` location: gateway-level (`~/.opencomputer/bindings.yaml`)
- CLI namespace: `oc bindings` (not `oc agents bind`)

## Audit history

This spec went through two adversarial-audit passes before
implementation began. Findings + fixes are in the implementation plan:

- **Pass 1** (self-audit) — 10 gaps (G1-G10): construction-time path
  capture, plugin-filter wiring, DelegateTool closure, test-timing
  flake, subagent ContextVar propagation, log-line `profile_id`,
  documentation gaps. All fixed inline before Pass 2.
- **Pass 2** (independent Opus subagent against the codebase) — 12
  further findings (F1-F12): wrong `AgentLoop` signature in plan,
  invented helper APIs, silent-no-match for unsupported binding
  fields, `WireServer` bypass, consent-gate prompt-handler scope,
  `PluginAPI` default-frozen paths, phase ordering, long-running
  task ContextVar contract. HIGH/MEDIUM fixed in plan; LOW
  documented as known limitations or v1.1 follow-ups.

The plan (`docs/superpowers/plans/2026-04-30-profile-as-agent-multi-routing.md`)
is the canonical implementation reference.

Next step: execution via `superpowers:executing-plans`.
