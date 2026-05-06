# Social Traces Plugin — Implementation Plan

> **Status:** design complete, ready to implement
> **Last updated:** 2026-05-05
> **Owner:** archits01
> **Sibling doc:** [`openhub-mvp.md`](./openhub-mvp.md) — the network this plugin talks to
> **Source brief:** `~/Downloads/HANDOVER.md` (read first if returning fresh)

---

## 0. Read-this-first context

If you're picking this up cold (new system, lost session, fresh Claude Code window):

1. Open `~/Downloads/HANDOVER.md` for the original product pitch — what we're building and why.
2. Read [§1 What this is](#1-what-this-is) and [§2 The flow](#2-the-flow) below to understand the design.
3. Read [§4 Decision log](#4-decision-log-everything-settled-in-session-2026-05-05) — every architectural call we've made and the reasoning. Don't re-litigate these without revisiting that section.
4. Read [§9 Open questions](#9-open-questions-still-tbd) for what's still TBD.
5. The next concrete action is in [§10 Implementation phases — what to do next](#10-implementation-phases). Pick the next un-checked Phase.

The companion doc [`openhub-mvp.md`](./openhub-mvp.md) describes the network this plugin talks to. The two are designed in lockstep but are separate repos and separate work streams. **Build this plugin first against the local-file backend** — it works end-to-end without OpenHub existing.

---

## 1. What this is

A bundled OpenComputer plugin (`extensions/social-traces/`) that gives every OC agent native participation in a collective knowledge network of structured task traces. It's the "agents discover what other agents have done, and contribute back" feature from `HANDOVER.md`.

**Two halves:**

- **This plugin** — the agent side. Lives in OpenComputer. Pre-task query, post-task emit, privacy redaction, distillation.
- **OpenHub** — the network side. Separate repo. HTTP API + Postgres + admin review. See [`openhub-mvp.md`](./openhub-mvp.md).

They meet at a typed HTTP boundary (`TraceNetworkClient` ABC + `TraceCard` dataclass, both in `plugin_sdk/`).

## 2. The flow

```
        user message arrives
              │
              ▼
      ┌──────────────────┐
      │ Pre-task hook    │
      │ build (intent,   │
      │ tags) query      │
      └────────┬─────────┘
               │
               ▼
        OpenHub.query()
        (1s soft timeout;
         on failure → empty)
               │
       ┌───────┴───────┐
       │               │
       ▼               ▼
   trace            no trace
   found            found
       │               │
       ▼               ▼
  inject as        agent
  <trace>          explores
  context          from
       │           scratch
       ▼               │
   agent              │
   executes           │
       │               │
       ▼               ▼
   END OF TASK    END OF TASK
       │               │
       ▼               ▼
  Post-task subscriber reads session_state bridge (pop_session)
       │               │
       ▼               ▼
   trace_used      trace_used
   is set          is None
       │               │
       ▼               ▼
   LLM novelty     emit unconditionally
   judge:          (genuinely new
   is_novel?       to the network)
       │
   ┌───┴────┐
   │        │
   yes      no
   │        │
   ▼        ▼
 emit    silent
   │
   ▼
 redact PII
   │
   ▼
 distill (3 Haiku calls)
   │
   ▼
 OpenHub.submit()
 (or queue locally if down)
```

## 3. Why this shape

Two security invariants drive the design (from HANDOVER.md):

1. **The network never sees raw user data.** Privacy redaction is the agent's job, before submission. Admin review is the second line, not the first.
2. **The agent never trusts network responses.** TraceCards are read as REFERENCE only — `distilled_insight` is text in context, `steps` are formatted as text, never auto-executed. This is the prompt-injection mitigation built into the schema.

Every component below exists because of one of these two rules. Don't relax either without explicit re-litigation.

## 3.5 Scope — v1 vs v1.1

This plan covers two of the three trace surfaces from HANDOVER:

| Surface | Scope | Notes |
|---|---|---|
| Pre-task lookup (mid-task injection) | **v1** | The high-leverage moment — biggest token savings |
| Post-task emit (TraceCard distillation) | **v1** | Closes the contribution loop |
| Morning feed (proactive ambient discovery) | **v1.1 — deferred** | See §13 for design + deferred phase. Surface-design questions are best answered after using v1 for a couple weeks |

Build v1 end-to-end (Phases 0-12), demo it, get the flywheel turning. Then layer morning feed on top as v1.1.

## 4. Decision log (everything settled in session 2026-05-05)

| Decision | Choice | Reasoning |
|---|---|---|
| Where this lives | Bundled plugin in `extensions/social-traces/` | Functionally core when shipped; matches `skill-evolution`/`memory-honcho`; respects the SDK boundary |
| Pre→post task signal | Runtime flag: `runtime.custom["trace_used"] = trace_id_or_None` | Simpler than bus event; per-task scope is exactly what we need |
| Novelty rule | **(d) Binary + LLM judge only when trace was used** | Free on the common (no-trace) path; pays one Haiku call per session only when there's a trace to compare against |
| Distillation | Three-Haiku LLM extraction, mirroring `extensions/skill-evolution/skill_extractor.py` | Plumbing + cost guard already exist in skill-evolution |
| Tag taxonomy | Free-form (LLM-generated) for v1; normalization deferred to network side | Network can collapse synonyms later without plugin changes |
| Trace versioning | Implicit via curation engine score | Schema stays lean; new better trace just gets a higher score |
| Network unreachable | Soft 1s timeout, fall through to explore (treat as "no trace found") | Agent never paralyzed by network outage |
| Submission queue | Local outbox if network down, drained on next successful run | Submissions don't get lost during outages |
| Privacy redaction | Agent-side, before submission | Network never sees raw user data |
| Pre-task hook seam | New `BEFORE_TASK` hook event in `plugin_sdk.HookEvent` | Existing `USER_PROMPT_SUBMIT` is fire-and-forget; injection providers freeze with the system prompt — neither fits |
| Novelty rule alternatives rejected | (a) binary-only — "loses 'I found a better way' signal, network stagnates"; (b) signals-only proxy (loop_count, token_cost) — "self-report vs self-report, false-positive city"; (c) pure LLM judge always — "pays Haiku on every session including empty-network sessions" | See full reasoning in §A — Comparison of novelty rules below |
| Network as bundled plugin vs core | Bundled plugin | Every comparable feature in OC ships as a bundled plugin; "core" effectively means "loaded by default" |

## 5. Where this fits in OpenComputer (existing structures we build on)

| OC primitive | File | What we use it for |
|---|---|---|
| Hook engine | [`opencomputer/hooks/engine.py`](../../opencomputer/hooks/engine.py) | Register `BEFORE_TASK` (blocking) and reuse `SessionEnd` (fire-and-forget) |
| TypedEventBus + `SessionEndEvent` | [`opencomputer/ingestion/bus.py`](../../opencomputer/ingestion/bus.py), [`plugin_sdk/ingestion.py`](../../plugin_sdk/ingestion.py) | Subscribe for the post-task emission path |
| PluginAPI | [`opencomputer/plugins/loader.py:929-1032`](../../opencomputer/plugins/loader.py) | `register_hook`, `register_injection_provider`, etc. |
| Plugin manifest pattern | [`extensions/skill-evolution/plugin.json`](../../extensions/skill-evolution/plugin.json) | Template for `plugin.json` |
| Skill-evolution subscriber | [`extensions/skill-evolution/subscriber.py`](../../extensions/skill-evolution/subscriber.py) | Almost-exact template for our post-task subscriber |
| Skill-evolution extractor | [`extensions/skill-evolution/skill_extractor.py`](../../extensions/skill-evolution/skill_extractor.py) | Template for the three-Haiku distillation |
| Skill-evolution candidate store | [`extensions/skill-evolution/candidate_store.py`](../../extensions/skill-evolution/candidate_store.py) | Template for the local outbox |
| Episodic memory | [`opencomputer/agent/episodic.py`](../../opencomputer/agent/episodic.py) | Same data (user/assistant/tools per turn) we'll feed the distiller |
| Run-conversation hook firing | [`opencomputer/agent/loop.py:685`](../../opencomputer/agent/loop.py) (USER_PROMPT_SUBMIT) and [`:1737`](../../opencomputer/agent/loop.py) (record_turn) | Fire points we model on |
| Session end emission | [`opencomputer/agent/loop.py:2687`](../../opencomputer/agent/loop.py) | Where SessionEndEvent gets published |
| Plugin SDK boundary | [`plugin_sdk/__init__.py`](../../plugin_sdk/__init__.py) | We add to public exports here |

## 6. Components — file-by-file

### 6.1 Additions to `plugin_sdk/` (the public contract)

These are stable types both this plugin and OpenHub consume. Versioned, frozen.

```
plugin_sdk/
├── traces.py                  ← NEW
│   ├── TraceCard              (frozen dataclass — wire format)
│   ├── TraceMeta              (tags, outcome, token_cost, loop_count, harness_version)
│   ├── TraceStep              (tool_call name + arguments_summary + result_summary)
│   ├── TraceNetworkClient     (ABC: query / submit / health)
│   ├── SubmitReceipt          (returned by submit())
│   ├── QueryResult            (returned by query())
│   └── TRACE_API_V1 = "v1"    (API version constant)
│
└── hooks.py                   ← MODIFIED
    └── HookEvent              (add BEFORE_TASK = "BeforeTask")
```

`plugin_sdk/__init__.py` exports all the new public names. Add a section comment so they're easy to find.

### 6.2 New plugin: `extensions/social-traces/`

```
extensions/social-traces/
├── plugin.json                ← manifest (id, version, kind=mixed, entry=plugin)
├── plugin.py                  ← register(api): wire hooks + subscriber
├── README.md                  ← user-facing usage
│
├── prefetch.py                ← pre-task hook handler
│   ├── build_query()          (extract intent + tags from user message)
│   ├── score_traces()         (which trace clears the relevance bar)
│   ├── format_injection()     (assemble <trace>...</trace> block)
│   └── on_before_task()       (the hook handler)
│
├── tag_extractor.py           ← derive tags from session context
│   ├── extract_tags_from_message()
│   └── (tag_profile state — accumulated tag profile per profile_home)
│
├── subscriber.py              ← post-task: SessionEndEvent subscriber
│   ├── EmissionSubscriber     (mirrors EvolutionSubscriber)
│   ├── _is_enabled()
│   └── _run_pipeline()        (decide → judge? → redact → distill → submit)
│
├── novelty_judge.py           ← LLM judge for "did agent improve on the trace?"
│   └── judge_novelty_async()  (cost-guarded, returns is_novel: bool)
│
├── distiller.py               ← three-Haiku trace distillation
│   ├── distill_intent()       (Haiku call 1)
│   ├── distill_steps()        (Haiku call 2)
│   ├── distill_insight()      (Haiku call 3)
│   └── extract_trace_card()   (orchestrator returning TraceCard or None)
│
├── redactor.py                ← privacy: PII, paths, hostnames, secrets
│   ├── REDACTED, REDACTED_PII (sentinels — match skill-evolution conventions)
│   ├── redact()               (regex + caller-supplied filter)
│   └── _PATH_RE, _CC_RE, _SSN_RE  (regex set; lift from skill_extractor.py)
│
├── client/
│   ├── __init__.py            ← exposes default factory based on config
│   ├── local_file.py          ← LocalFileTraceNetworkClient (dev stub)
│   └── http.py                ← HttpTraceNetworkClient (httpx, talks to OpenHub)
│
├── outbox.py                  ← local queue when network is down
│   ├── enqueue()              (write JSON to <profile_home>/traces/outbox/)
│   ├── drain()                (try to submit each, remove on success)
│   └── drain_periodically()   (called from subscriber when network is back)
│
├── cache.py                   ← optional local query cache (perf)
│   └── LRU on (intent_hash, tag_set_hash) → list[TraceCard]
│
├── identity.py                ← per-profile opaque agent id (submitter_hash)
│   └── get_or_create_agent_id(profile_home: Path) → str
│
└── config.py                  ← read social_traces section from config.yaml
    └── SocialTracesConfig     (backend, endpoint, enabled, redaction toggle)
```

### 6.3 Plugin config (in user's `~/.opencomputer/<profile>/config.yaml`)

```yaml
social_traces:
  enabled: true
  backend: local | http        # local-file (dev), http (production)
  endpoint: http://localhost:8000   # only when backend=http
  agent_id_path: traces/agent_id    # opaque id — never user identity
  privacy:
    redact_paths: true
    redact_hostnames: true
    extra_redactors: []        # callable filter ids registered by other plugins
  novelty_judge:
    enabled: true              # rule (d) — set false to use rule (a)
    cost_guard_usd_per_session: 0.01
  query:
    soft_timeout_s: 1.0
    top_k: 3
    relevance_threshold: 0.6   # below this, treat as "no trace found"
  outbox:
    max_pending: 100           # cap to avoid runaway disk growth
```

## 7. Data structures

### 7.1 TraceCard (the wire format)

Lives in `plugin_sdk/traces.py`. Both this plugin and OpenHub serialize against it.

```python
@dataclass(frozen=True, slots=True)
class TraceMeta:
    tags: tuple[str, ...]              # ("homelab", "filesync", "lan")
    outcome: Literal["success", "partial", "failed"]
    token_cost: int
    loop_count: int
    harness_version: str               # opencomputer.__version__
    submitter_hash: str                # opaque per-agent id

@dataclass(frozen=True, slots=True)
class TraceStep:
    tool_name: str                     # "Bash", "Read", etc. — never executed
    arguments_summary: str             # redacted, summarized
    result_summary: str                # redacted, summarized
    duration_ms: int

@dataclass(frozen=True, slots=True)
class TraceCard:
    schema_version: str                # "v1"
    intent: str                        # "sync files between two machines on LAN"
    meta: TraceMeta
    steps: tuple[TraceStep, ...]
    distilled_insight: str             # the agent's takeaway
    created_at: str                    # ISO-8601 UTC
    # Server-side fields (None on submit, set by network):
    id: str | None = None
    status: Literal["pending", "approved", "rejected", "superseded"] | None = None
    score: float | None = None
```

### 7.2 Pre→post-task bridge

The natural-looking option (write to `runtime.custom["trace_used"]` in the pre-task hook, read in the post-task subscriber) does not work: the agent loop swaps `runtime` via `dataclasses.replace` so per-hook writes don't survive, and `SessionEndEvent` strips `runtime` entirely before publishing. There is no path from `runtime.custom` at pre-task time to the subscriber at post-task time — they're in different worlds.

Solution (Phase 5, option a — see Appendix A): a process-wide module-level dict in `extensions/social-traces/session_state.py`, keyed by `session_id`, lock-guarded with LRU eviction.

Set by `prefetch.on_before_task()`:

```python
session_state.set_trace_used(session_id, trace_id_or_None, trace_card=card)
```

Read + cleared atomically by `subscriber._run_pipeline_body()`:

```python
entry = session_state.pop_session(session_id)
# entry.trace_used, entry.trace_card, entry.hit_count
```

The `runtime.custom["trace_used"]` write was tried in Phase 4, found inert in Phase 9.A.2, and removed.

### 7.3 OpenHub query/response shapes

```python
@dataclass(frozen=True, slots=True)
class QueryResult:
    traces: tuple[TraceCard, ...]      # top-K
    query_id: str                      # for telemetry
    served_from: Literal["network", "cache"]

@dataclass(frozen=True, slots=True)
class SubmitReceipt:
    accepted: bool
    queue_id: str | None               # set if accepted
    reason: str                        # error reason if rejected at validation
```

## 8. Hook integration

### 8.1 New hook event: `BEFORE_TASK`

Fires after `USER_PROMPT_SUBMIT` but before the first LLM call. Blocking (so we can synchronously inject the trace). HookContext carries the user message text.

Add to `plugin_sdk/hooks.py`:

```python
class HookEvent(str, Enum):
    ...existing...
    BEFORE_TASK = "BeforeTask"   # NEW
```

Add to `ALL_HOOK_EVENTS` tuple in declaration order.

### 8.2 Where the loop fires it

In [`opencomputer/agent/loop.py`](../../opencomputer/agent/loop.py), `run_conversation()`. After the existing `USER_PROMPT_SUBMIT` fire-and-forget (around line 685) and after the slash-command early-return guard (around line 856), but before the system-prompt build:

```python
# NEW: BEFORE_TASK — blocking. Plugin pre-fetches traces and may
# inject a <trace>...</trace> system reminder via HookDecision.modified_message.
ctx = HookContext(
    event=HookEvent.BEFORE_TASK,
    session_id=sid,
    runtime=self._runtime,
    message=Message(role="user", content=user_message),
)
decision = await hook_engine.fire_blocking(ctx)
if decision is not None and decision.decision in ("rewrite", "approve") and decision.modified_message:
    # Inject as a user-side system reminder, same plumbing as the loop-
    # detector warning at loop.py:1907-1920.
    reminder = Message(
        role="user",
        content=f"<system-reminder>{decision.modified_message}</system-reminder>",
    )
    messages.append(reminder)
    self._emit_before_message_write(session_id=sid, message=reminder)
    self._persist_message(sid, reminder)
```

The plugin's `prefetch.on_before_task()` returns a `HookDecision` with `modified_message=` set to the formatted `<trace>` block (or `pass` if no trace).

### 8.3 Post-task: bus subscriber on SessionEndEvent

No new hook needed. Subscribe to the existing `SessionEndEvent` published by [`loop.py:2687 _emit_session_end_event()`](../../opencomputer/agent/loop.py).

In `extensions/social-traces/subscriber.py`, mirror `EvolutionSubscriber` exactly — same lifecycle, same fire-and-forget pipeline, same on-disk enabled state.

## 9. Open questions (still TBD)

| # | Question | When to settle |
|---|---|---|
| 1 | Tag taxonomy — keep free-form or introduce controlled vocab once network has ~100 traces? | After Stage 2 (Pi) deployment with real cross-machine traffic |
| 2 | Trust score / Phase-2 progressive auto-approval | Post-MVP — needs ~50 reviewed traces of training signal first |
| 3 | Anomaly detection for Phase 3 fully-automated review | Post-MVP |
| 4 | When/whether to add embedding-based intent matching (pgvector) instead of pure tag matching | When tag-match recall feels low in practice |
| 5 | Per-domain federation — single network or split by `#homelab` vs `#coding` etc. | Post-MVP — wait until usage patterns emerge |
| 6 | Should `BEFORE_TASK` be exposed in `plugin_sdk/__init__.py` for third-party plugins, or kept internal? | Before v1.0 SDK freeze |

## 10. Implementation phases

### Phase 0 — SDK additions (1-2 hours) — COMPLETE 2026-05-05

- [x] Add `BEFORE_TASK` to `plugin_sdk/hooks.py:HookEvent` and `ALL_HOOK_EVENTS`
- [x] Create `plugin_sdk/traces.py` with `TraceCard`, `TraceMeta`, `TraceStep`, `TraceNetworkClient` ABC, `SubmitReceipt`, `QueryResult`, `TRACE_API_V1`
- [x] Export all new names from `plugin_sdk/__init__.py`
- [x] Add `tests/plugin_sdk/test_traces.py` — schema serialization round-trip, ABC enforcement (19 tests)
- [x] Confirm SDK boundary test still passes (`tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer`)
- [x] Update existing pinned-count tests (`test_hook_expansion.py`, `test_phase11b.py`) — bumped 20 → 21

### Phase 1 — Loop integration (1 hour) — COMPLETE 2026-05-05

- [x] Wire `BEFORE_TASK` fire in `opencomputer/agent/loop.py` (location: immediately after the user message is appended + persisted, around line 1058 — the seam is "before the agent starts a real task")
- [x] Test: a registered hook returning `decision="approve", modified_message="..."` causes a `<system-reminder>` user message to land in `messages` and persist to the DB
- [x] Test: existing `run_conversation` flow unaffected when no `BEFORE_TASK` hook is registered
- [x] Test: `decision="pass"`, `None` return, empty `modified_message` all → no reminder
- [x] Test: handler that raises does NOT break the loop (fail-open contract)
- [x] All 7 BEFORE_TASK integration tests pass; full hook+trace+loop+phase6a+phase11b sweep stays green (77 passed, 1 documented skip)

### Phase 2 — Plugin scaffold (2-3 hours) — COMPLETE 2026-05-05

- [x] Create `extensions/social-traces/plugin.json` (kind=mixed, enabled_by_default=false)
- [x] Create `config.py` — `SocialTracesConfig` frozen dataclass + `from_config_dict` parser with defaults
- [x] Create `identity.py` — per-profile opaque `submitter_hash` (32-byte hex, regenerable by deleting the file)
- [x] Create `state.py` — on-disk enabled flag + heartbeat under `<profile_home>/traces/`
- [x] Stub `prefetch.py` — Phase 2 contract: returns `pass`, writes heartbeat when enabled, records the session_state bridge entry with `trace_used=None`
- [x] Stub `subscriber.py` — `TraceEmissionSubscriber` shape with start/stop lifecycle (real pipeline pending Phase 5+)
- [x] `plugin.py:register(api)` — registers BEFORE_TASK hook with priority=20, timeout_ms=1500
- [x] Add `oc traces enable/disable/status` CLI surface — top-level namespace via `opencomputer/cli_traces.py`, mounted in `cli.py`
- [x] Privacy-respecting status output — never includes session_id, intent, or distilled content
- [x] `extensions/social-traces/README.md` — user-facing doc with two-layer opt-in instructions
- [x] Smoke test: `opencomputer.plugins.discovery.discover()` lists `social-traces`; CLI verbs round-trip the on-disk flag
- [x] 18 new tests in `tests/test_social_traces_phase2.py` — manifest, state, identity, config, prefetch stub semantics, plugin.register registration

### Phase 3 — Local-file backend (2-3 hours) — COMPLETE 2026-05-05

- [x] `client/__init__.py` + factory `make_client(backend, profile_home, endpoint)` — selects local/http; http path raises `NotImplementedError` until Phase 9
- [x] `client/local_file.py` — implements `TraceNetworkClient` over `<profile_home>/traces/{inbox,outbox}/` JSON files
  - `query()`: scans `inbox/*.json`, scores by tag-overlap + intent-word-overlap (with success-outcome tiebreaker), returns top-K
  - `submit()`: writes `outbox/<queue_id>.json`, stamps id+status="pending" so the on-disk shape matches what OpenHub would return
  - `health()`: returns True if directory writable; soft timeout via `asyncio.wait_for`
- [x] Soft timeout (`timeout_s` kwarg) on query + health — slow IO surfaces as empty/False, never raised
- [x] All filesystem I/O wrapped in `asyncio.to_thread` so the contract holds in a real running agent
- [x] Inbox helpers used by CLI: `list_inbox`, `show_inbox`, `add_to_inbox`, `remove_from_inbox`, `list_outbox`
- [x] `add_to_inbox` validates JSON parses as TraceCard before copying — fail-fast at CLI time
- [x] CLI: `oc traces inbox {add,list,show,remove}` + `oc traces outbox {list,show}`
- [x] Score formula: tag/word-overlap is the qualifier; outcome weight is a tiebreaker only — prevents every success trace from matching every query
- [x] Smoke test: full add → list → show → remove → list cycle round-trips correctly with `OPENCOMPUTER_PROFILE_HOME` override
- [x] 27 new tests in `tests/test_social_traces_phase3.py` — factory, query (top-K, malformed-skip, soft-timeout), submit (round-trip, failure receipt), health, inbox/outbox helpers, score_trace ordering
- [x] 122/122 tests green across affected files (Phase 0/1/2/3 + SDK boundary + hook expansion + plugin manifest)

### Phase 4 — Pre-task hook (3-4 hours) — COMPLETE 2026-05-05

- [x] `tag_extractor.py:extract_tags_from_message()` — v0 keyword extraction (lowercase + alphanumeric-only + stopword filter + min-length + dedupe + max cap; LLM upgrade in Phase 8)
- [x] `prefetch.py:build_query()` — intent = user message verbatim (truncated to 500 chars) + tags from extractor
- [x] `prefetch.py:select_best_trace()` — picks the top-scored trace iff its score clears `query.relevance_threshold`; returns None otherwise (caller treats identically to "empty result")
- [x] `prefetch.py:format_injection()` — renders the trace as `<trace intent="..." outcome="..." tags="...">Insight: ...\nSteps used (reference only): 1. ToolName: args → result\n...</trace>` with explicit "do not auto-execute" framing
- [x] `prefetch.py:on_before_task()` — full handler: read enabled flag → write heartbeat → build query → call `client.query()` with config-driven timeout → score gate → format → return `HookDecision(decision="rewrite", modified_message=...)` if injecting, `pass` otherwise
- [x] `_load_config()` reads `social_traces:` from `<profile_home>/config.yaml` (defaults if missing/malformed)
- [x] `session_state` bridge stamped with `trace_id`-or-`None` for every fire — see Phase 5 note below for why the runtime.custom path was a dead end
- [x] Local-file backend stamps `score` on returned cards (so prefetch's threshold gate has a server-supplied signal to read)
- [x] Failure isolation: any exception in the handler logs at DEBUG/WARNING and falls through to `pass` — agent never paralysed by prefetch
- [x] Integration test (load-bearing): seed inbox → run agent through `AgentLoop.run_conversation` → `<trace>` block lands as `<system-reminder>` user message + persists to SessionDB
- [x] Mirror integration test: non-matching message produces no injection, no `<trace>` in messages
- [x] 25 new tests in `tests/test_social_traces_phase4.py` — tag extractor, build_query, select_best_trace, format_injection, on_before_task variants, end-to-end seeded + no-match
- [x] 147/147 affected-file tests green (Phases 0-4 + SDK boundary + hook expansion + plugin manifest); 1 documented skip

#### Phase 5 design finding — `runtime.custom` won't bridge pre→post-task

The plan §4 originally said "post-task code reads `runtime.custom['trace_used']`" set by the pre-task hook. **That doesn't work as written.** Two reasons surfaced in the Phase 4 end-to-end test:

1. The agent loop calls `dataclasses.replace(self._runtime, custom={...})` on entry to `run_conversation` (loop.py:~775) to thread `session_id`/`session_db`. That creates a NEW custom dict — mutations from inside `BEFORE_TASK` go to the loop's internal dict, not back to the caller's `RuntimeContext`.
2. `SessionEndEvent` (the typed-bus payload subscribers receive) carries only `session_id`, `end_reason`, `turn_count`, `duration_seconds`, `had_errors` — no runtime, no custom. The post-task subscriber has no way to see what the pre-task hook set.

**Three options for Phase 5 to pick from:**

- **(a) Module-level dict keyed by session_id.** `extensions/social-traces/session_state.py` — simplest, dev-fast. Lost on process restart (but so is `runtime.custom`).
- **(b) SessionDB metadata column.** Add `social_trace_used: text | null` to the `sessions` table; pre-task writes, subscriber reads. Durable, survives restart.
- **(c) Add a `trace_used` field to `SessionEndEvent`.** Cleanest signal flow but requires plugin_sdk schema change and a way for the agent loop to populate it from the per-task runtime — not lightweight.

Recommendation: **(a) for v1.0**, revisit (b) if the in-memory state proves fragile under daemon-mode use. Decision deferred until Phase 5 implementation actually starts.

### Phase 5 — Post-task subscriber (3-4 hours) — COMPLETE 2026-05-05

- [x] **Bridge mechanism (option a from §10 Phase 4 finding):**
  `extensions/social-traces/session_state.py` — module-level dict
  keyed by session_id with `set/peek/pop/known/hit_count` API,
  `RLock`-guarded for thread safety, LRU eviction past a soft cap so
  daemon-mode can't leak memory
- [x] Prefetch hook now writes the bridge in addition to
  `runtime.custom["trace_used"]` — closes the propagation gap from
  Phase 4
- [x] `novelty_judge.py` Phase 5 stub — returns `is_novel=False`
  unconditionally (conservative default; Phase 6 swaps for the real
  Haiku call)
- [x] `distiller.py` Phase 5 stub — returns `None` unconditionally
  (no submissions land in the outbox until Phase 7 implements the
  three-Haiku flow)
- [x] `subscriber.py` real `TraceEmissionSubscriber` (replaces Phase
  2 stub):
  - `start/stop` lifecycle subscribes to `session_end` on the typed
    bus; idempotent on both ends
  - `_handle_event` is bus-facing + fast: reads enabled flag, writes
    heartbeat, fires the heavy pipeline as `fire_and_forget`
  - `_run_pipeline` runs the rule (d) decision tree:
    - flag off → pop bridge entry (no leak), return
    - session unknown (BEFORE_TASK never fired) → return
    - `trace_used set + novelty_judge.enabled=False` → silent
    - `trace_used set + judge.is_novel=False` → silent
    - `trace_used set + judge.is_novel=True` → distill + submit
    - `trace_used None` (explored from scratch) → distill + submit
    directly
  - Bridge is ALWAYS popped by the end of the pipeline (memory-leak
    guard tested)
  - Every stage wrapped in try/except — fire-and-forget contract
- [x] `plugin.py` updated to start the subscriber at register time;
  uses `default_bus` from `opencomputer.ingestion.bus`. Lazy
  factories (`profile_home_factory`, `client_factory`,
  `config_factory`) so multi-profile dispatch and live config
  reloads both work
- [x] `oc traces status` extended to show `tracked sessions: N`
  (aggregate count only — never individual session ids)
- [x] CLI alias bootstrap extended to load all the new modules
  (`session_state`, `tag_extractor`, `novelty_judge`, `distiller`)
- [x] 22 new tests in `tests/test_social_traces_phase5.py`:
  - bridge: known/unknown distinction, none-vs-unknown semantics,
    pop clears, hit_count, LRU eviction past cap, thread-safety
    smoke test
  - prefetch writes the bridge on match + on no-match
  - subscriber lifecycle: start subscribes, idempotent, stop
    unsubscribes
  - decision tree (paths a-h): disabled-skip, untracked-skip,
    judge-not-novel-silent, judge-novel-continues-to-distill,
    no-trace-skips-judge, distiller-None-no-submit, distiller-
    proposal-triggers-submit, submit-rejected-isolated, distiller-
    raises-isolated, judge-disabled-config-silent
  - memory-leak guard: bridge always popped after pipeline
- [x] 169/169 affected-file tests green across Phases 0-5; 1
  documented skip
- [x] CLI smoke test: `oc traces enable && oc traces status` round-
  trips and shows the new `tracked sessions` field

### Phase 6 — Novelty judge (2-3 hours) — COMPLETE 2026-05-06

- [x] `novelty_judge.judge_session_novelty` — real Haiku call mirroring `extensions/skill-evolution/pattern_detector.py:judge_candidate_async`:
  - System prompt with three-signal calibration (improvement / edge case / different route)
  - Structured JSON output: `{novel, confidence, reason}`
  - JSON parser tolerates markdown fences + surrounding prose
  - Confidence clamped to [0, 100]
  - Cost-guard pre-flight via `check_budget` (accepts both bool + BudgetDecision shapes); skips provider call on denial
  - `record_usage` after successful response (signature-tolerant for test mocks)
  - Transcript truncated to 4000 chars before sending (Haiku context budget)
  - Provider raise / parse failure / no-provider all fall to `is_novel=False` (conservative default)
- [x] `NoveltyVerdict` extended with `confidence` field
- [x] `session_state` bridge extended to carry the full `TraceCard` body in `_SessionEntry.trace_card` — judge reads it without re-querying the network
- [x] `prefetch.on_before_task` writes the chosen card to the bridge alongside its id
- [x] `subscriber.TraceEmissionSubscriber` constructor accepts optional `provider` + `cost_guard`; threads them through `_judge_novelty`
- [x] `subscriber._read_session_for_judge` pulls the user message + transcript from `SessionDB.get_messages(session_id)` at session-end time; filters out our own `<system-reminder>` injections so the judge sees the agent's work, not the trace we injected
- [x] Boundary inventory refreshed for `opencomputer.agent.state.SessionDB` import (lazy, inside the read helper) — same pattern skill-evolution uses for its provider/db lookups
- [x] Earlier `opencomputer.agent.config._home` import removed in favour of `state.resolve_profile_home` (plugin_sdk + stdlib only) — net inventory delta is one new entry, one removed
- [x] **Production wiring deferred to Phase 9** — `plugin.register()` still passes `provider=None`/`cost_guard=None` so the judge degrades to `is_novel=False` until the gateway-side bootstrap (mirroring `_start_evolution_subscriber`) lands. CLI single-shot path (`opencomputer chat`) doesn't fit the long-lived subscriber model and intentionally never wires real LLM calls
- [x] 21 new tests in `tests/test_social_traces_phase6.py`:
  - parser: bare JSON, markdown fences, surrounding prose, malformed, empty, confidence clamping, missing-fields defaults
  - judge API: no-provider degrades, novel/not-novel verdicts, cost-guard denial skips provider, record_usage after success, provider raises → not-novel, parse failure → not-novel, transcript truncation
  - `_budget_allows` accepts both bool + dataclass shapes
  - subscriber threads provider+cost_guard through to the judge
  - subscriber reads SessionDB transcript at session-end (with system-reminder filtering)
  - judge novel→continues to distill (Phase 5 stub returns None)
  - no-provider in subscriber → degraded → silent (no distill call)
  - prefetch writes the full TraceCard to the bridge (Phase 6 contract)
- [x] 194/194 affected-file tests green (Phases 0-6 + SDK boundary + hook expansion + plugin manifest + extension boundary), 1 documented skip

### Phase 7 — Redactor + distiller (3-4 hours) — COMPLETE 2026-05-06

- [x] `redactor.py` (new) — comprehensive privacy redaction with layered defence:
  - Always-on PII layer: credit cards, SSNs, emails, US-shaped phone numbers
  - Always-on secrets layer: OpenAI/Anthropic `sk-`, GitHub `ghp_`/`ghs_`/`gho_`/`ghu_`/`ghr_`, Google `AIza`, Slack `xox[abprs]-`, Bearer tokens, password=… assignments
  - Opt-in path layer: POSIX abs paths (`/Users`, `/home`, `/var`…), tilde paths, Windows `C:\Users\…`. Relative paths (`src/foo.py`) NOT redacted to keep traces useful
  - Opt-in hostname layer: URLs, internal-shaped hosts (`*.local`, `*.lan`, `*.home`, `*.internal`, `*.corp`, `*.test`, `*.dev`), IPv4 + IPv6. Public TLDs like `github.com` deliberately survive
  - Caller filter: operator-supplied `sensitive_filter` callable; whole-body match → collapse to sentinel; raises also redact
  - `is_useful_body()` rejects sentinel-only / too-short content so distiller drops cards where redaction nuked the substance
- [x] `distiller.py` real implementation (replaces Phase 5 stub):
  - Three Haiku calls: `_distill_intent` (≤80 tokens) → `_distill_steps` (≤600 tokens, JSON list) → `_distill_insight` (≤300 tokens)
  - Each call cost-guarded via `cost_guard.check_budget`; budget denial short-circuits the pipeline
  - Two-pass redaction per call: input prompt + LLM output. Defense-in-depth — model may emit a path even when prompt asks it not to
  - Tolerant JSON parser for steps (handles markdown fences + surrounding prose)
  - Tags derived via existing `tag_extractor.extract_tags_from_message`, then normalized to lowercase alphanumeric+hyphen with length 2-30, max 10 (matches `openhub-mvp.md` §8.3 server-side validation)
  - Schema validation `_validate()` mirrors server validation rules; any failure → drop card
  - Outcome sourced from caller (`SessionEndEvent.had_errors`), not from message content — `is_error` flag lives on `ToolResult` and is lost by the time messages persist to SessionDB
  - Final `TraceCard` carries `schema_version=v1`, server-side fields (`id`, `status`, `score`) left None for OpenHub to stamp
- [x] Subscriber updates:
  - Constructor takes optional `sensitive_filter` + `harness_version` alongside `provider`/`cost_guard`
  - Threads all four into `distill_session` plus `outcome` from `event.had_errors`
- [x] Boundary inventory refreshed for `opencomputer.agent.state.SessionDB` lazy import in distiller
- [x] 63 new tests in `tests/test_social_traces_phase7.py`:
  - Redactor: every regex pattern (positive + negative), pipeline ordering, layer toggles, caller-filter precedence, sentinel handling, `is_useful_body` semantics
  - Distiller helpers: `_normalize_tags` (length / lowercase / dedupe / cap), `_parse_steps_json` (bare / markdown / prose / invalid), `_validate` (intent length, insight length, empty tags, empty steps, short submitter_hash)
  - Per-call: intent redacts output, intent skipped on filter-redacted input, steps parses JSON list, steps fails-soft on parse failure, insight redacts output
  - Cost guard: pre-flight denies → no provider call, record_usage after success
  - Orchestrator: no-provider → None, no-user-message → None, full happy-path round-trip with three canned LLM responses produces a valid TraceCard, intent failure aborts pipeline (no further calls), caller filter collapses whole input → None without LLM, validation failure drops card, outcome=failed when caller passes it, invalid outcome string defaults to success
- [x] Phase 5 mocks updated to accept `**_kw` for the new distiller kwargs
- [x] 257/257 affected-file tests green across Phases 0-7 + SDK + extension boundary; 1 documented skip

### Phase 8 — LLM tag extractor (2-3 hours)

- [ ] Replace keyword tag extraction with one Haiku call
- [ ] Cache tag extraction per session (don't re-run mid-session)
- [ ] Maintain `tag_profile` accumulator on disk so `prefetch.build_query()` can include profile-bias tags

### Phase 9.A — Production wiring for Phases 6/7 (1-2 hours) — COMPLETE 2026-05-06

Promoted from a sub-bullet of Phase 9 to its own milestone — without this, the LLM judge + distiller never fire in real CLI / gateway use (they degrade to ``provider=None`` because ``plugin.register()`` had no way to resolve a real provider). After this commit the local single-machine flywheel actually works end-to-end.

- [x] `plugin.register()` no longer auto-starts a degraded subscriber. It registers ONLY the BEFORE_TASK hook (mirrors `extensions/skill-evolution/plugin.py`'s lifecycle-free shape)
- [x] `plugin.wire_subscriber(provider, cost_guard, sensitive_filter=None, harness_version="")` exported function — the canonical entry point gateway + CLI both call. Idempotent: stops a prior subscriber before constructing a new one
- [x] `plugin.stop_subscriber()` and `plugin.get_active_subscriber()` for shutdown / diagnostic
- [x] `opencomputer/gateway/server.py` — added `_start_traces_subscriber` mirroring `_start_evolution_subscriber`. Resolves `cfg.model.provider` against the live plugin registry, wraps it with the per-profile `get_default_guard()`, calls `wire_subscriber`. Failure-isolated. Mounted in `start()` after `_start_evolution_subscriber`. Stop hook in `Gateway.stop()` calls the plugin's `stop_subscriber`
- [x] `opencomputer/cli.py` — `_run_chat_session` calls `wire_subscriber` after `AgentLoop` construction when `oc traces enable` flag is set. Same provider + cost_guard resolution; same failure isolation. Means single-shot `opencomputer chat` now emits traces too (not just gateway-mode)
- [x] Stage-1 heuristic gate in `subscriber.is_session_worth_distilling`:
  - `turn_count < 2` → skip (one-turn = user asked, agent answered, no tools to share)
  - `duration_seconds < 3` → skip (cancellation, tool-guard abort, instant exit)
  - Failure-mode sessions (`had_errors=True`) deliberately PASS the gate — edge-case traces are valuable per HANDOVER
  - Thresholds in module constants, not config — heuristics, not policy. Promote to config when real-world data shows the cap is wrong
- [x] Phase 5 + 6 tests updated — `SessionEndEvent` constructions now pass real `turn_count` + `duration_seconds` so the gate doesn't filter them
- [x] 11 new tests in `tests/test_social_traces_phase9_wiring.py`:
  - `register()` only attaches BEFORE_TASK; no auto-subscriber
  - `wire_subscriber` constructs + stores singleton
  - Idempotency: second `wire_subscriber` stops the first
  - `stop_subscriber` no-op on empty state, idempotent
  - Heuristic gate: passes real session, rejects zero-turn, one-turn, short-duration; passes failed sessions
  - Pipeline applies the gate (trivial sessions don't reach distiller; normal sessions do)
- [x] 268/268 affected-file tests green; 1 documented skip; extension boundary clean
- [x] CLI smoke test confirms `oc traces enable && oc traces status` round-trips correctly

### Phase 9.A.1 — Dogfood ergonomics (1-2 hours) — COMPLETE 2026-05-06

Pre-emptive fixes for the things that would bite during the "use it locally for a few days" plan, before they bit. Promoted from the post-9.A "missing" list:

- [x] **Subscriber concurrency cap** — `_run_pipeline` body now sits behind an `asyncio.Semaphore(_MAX_CONCURRENT_PIPELINES=2)`. A burst of session_end events serializes at the semaphore instead of fanning out into 4×N concurrent Haiku calls. Lazy-constructed on first use so it binds to the loop that actually dispatches events
- [x] `identity.rotate_agent_id(profile_home)` — force-regenerates the submitter_hash, returns `(old_id, new_id)` so callers can echo before/after. Existing `get_or_create_agent_id` is a no-op when the file is present, so rotation needs its own entrypoint
- [x] `oc traces rotate-id` CLI verb — confirms before clobbering an existing id (`--yes`/`-y` skips); echoes truncated old + new
- [x] `oc traces dry-run <session_id> [--no-llm]` — runs the distill pipeline against an existing session, prints the resulting TraceCard JSON, never submits. `--no-llm` stays in the structural path (transcript fetch + redaction sweep + summary stats), incurs no provider cost. Lets you eyeball "what would my redactor produce on this real session" before turning emission on
- [x] `oc traces audit-redactor [--limit N] [--output PATH]` — sweeps the most-recent N sessions through the redactor, writes a before/after diff report file (default: `<profile_home>/traces/audit-<timestamp>.txt`). Output may contain raw user content; defaults to a profile-local file rather than stdout so it doesn't accidentally land in a chat paste-buffer
- [x] `oc traces status` extended — surfaces the in-process subscriber wiring (`subscriber: wired (provider=...)` vs `subscriber: not wired in this process`) plus the configured-provider field from `config.yaml`. Catches "I enabled traces but my config has no provider" before the user wonders why nothing's emitted
- [x] 12 new tests in `tests/test_social_traces_dogfood_fixes.py`:
  - `rotate_agent_id` creates / replaces / round-trips with `get_or_create_agent_id`
  - CLI `rotate-id` creates, replaces, aborts on no-confirm
  - CLI `status` shows subscriber-not-wired when called outside a long-lived process
  - CLI `dry-run --no-llm` reports redactions on a seeded session, handles missing session
  - CLI `audit-redactor` writes a before/after report file with the redacted PII inline
  - Subscriber semaphore: blocks past 2 concurrent pipelines (5 launched → peak ≤ 2)
  - `_run_pipeline` direct-call still works (back-compat for prior phase tests)
- [x] 280/280 affected-file tests green; 1 documented skip

### Phase 9.B — HTTP client + outbox (deferred to post-OpenHub)

These tasks land AFTER the OpenHub MVP exists (we need a real network endpoint to talk to):

- [ ] `client/http.py:HttpTraceNetworkClient` — httpx, async, talks to OpenHub
- [ ] Implements all three ABC methods; 1s soft timeout on query/health
- [ ] `outbox.py` — local queue when submit fails or network unreachable
- [ ] Outbox drain on next successful health() check (kick off from subscriber on each event arrival)

### Phase 10 — End-to-end demo (1-2 hours)

- [ ] Local OpenHub running on Mac (Stage 1 — see openhub-mvp.md)
- [ ] Two profiles: `oc -p alice`, `oc -p bob` against `http://localhost:8000`
- [ ] Walk through: alice solves novel task → submission → admin approve → bob queries → trace returned → bob uses it silently
- [ ] Document the demo flow in plugin's README

### Phase 11 — Tests + CI

- [ ] Unit tests for each module (target: 95%+ line coverage on `extensions/social-traces/`)
- [ ] Integration test: plugin loaded, hooks registered, full prefetch+emission cycle against local-file backend
- [ ] Boundary test: nothing in `extensions/social-traces/*.py` imports from `opencomputer.*` except where existing extensions already do (frozen inventory pattern from `tests/test_plugin_extension_boundary.py`)

### Phase 12 — Bundle + ship

- [ ] Add `social-traces` to default-disabled bundled extensions list
- [ ] Add `oc setup` wizard step asking the user to opt in
- [ ] Document in main `README.md` under "Bundled extensions"
- [ ] CHANGELOG entry

### Phase 13 — Morning feed (DEFERRED to v1.1)

The inbound ambient discovery surface from HANDOVER. Polled daily, surfaces interesting traces to the user proactively. Full design + open questions in [§13 v1.1 — Morning feed (deferred)](#13-v11--morning-feed-deferred). Concrete phases land once v1 is being used and surface-design feels real.

## 11. Tests — what coverage matters

- **Schema round-trip** — TraceCard JSON serialize → deserialize → equal
- **Privacy redaction** — known PII strings get redacted in both prompt input and LLM output
- **Trace injection** — `<trace>` block lands in `messages` as a user-side system reminder
- **Runtime flag persistence** — set in `BEFORE_TASK`, read in subscriber
- **Novelty judge gating** — when `trace_used` is None, judge never called; when set, judge always called
- **Outbox drain** — submission queued during outage drains on next successful health check
- **Soft timeout** — query that takes > 1s returns empty result, agent proceeds to explore
- **Plugin SDK boundary** — no `from opencomputer.*` in `plugin_sdk/traces.py`
- **Extension boundary** — no NEW `from opencomputer.*` in `extensions/social-traces/*.py` (use frozen inventory pattern)

## 12. Operations

### Enabling the plugin

```bash
oc traces enable                         # writes state.json {"enabled": true}
oc traces status                         # shows enabled flag + heartbeat ts + outbox depth
oc traces disable
```

### Inspecting

```bash
oc traces inbox                          # list traces fetched recently
oc traces outbox                         # list pending submissions
oc traces history --tag homelab          # local trace history
```

### Privacy

The `redactor.py` is the load-bearing privacy layer. The redaction inventory (regex set) should be reviewed before Stage 2 deployment. Test cases must include real-shape examples of:
- File paths with usernames
- Hostnames in URLs
- API keys / tokens
- Email addresses
- IPs (private + public)
- Phone numbers

If you can't justify a regex pattern matching nothing in your real session DB, the redactor is too loose.

---

## 13. v1.1 — Morning feed (deferred)

The inbound ambient discovery surface — agent polls OpenHub once daily for traces matching its tag profile, surfaces them to the user proactively without being asked. HANDOVER §Inbound calls this out as the "Twitter feed for agents" half of the network.

### Why this is deferred (not just "phase later")

Surface-design questions don't have textbook answers. Until v1 is in real use you don't know whether morning surfacing belongs in:
- A Telegram push?
- A CLI banner on next session start?
- A system notification?
- A dedicated `oc feed` view?
- All of the above, configurable?

Pre-building presenters that nobody'll use would be waste. Use v1 for a couple weeks, then design the surface with real signal.

### Components (sketch, not yet built)

```
extensions/social-traces/
├── feed.py                     ← daily poll + dedup + dispatch to surface
│   ├── poll_feed()             (daily fetch from /v1/feed)
│   ├── score_for_user()        (filter to "actually interesting")
│   ├── seen_set                (per-profile dedup of trace_ids surfaced)
│   └── surface()               (delegates to chosen presenter)
│
├── feed_scheduler.py           ← fires poll_feed once daily
│   └── (reuse OC's existing cron primitives — see opencomputer/tools/cron_tool.py)
│
└── feed_presenters/            ← multiple surfaces, configurable
    ├── cli_banner.py           (next session start: "I noticed X")
    ├── telegram_push.py        (gateway message)
    └── notification.py         (system notification)
```

Config additions in `social_traces:` block:

```yaml
morning_feed:
  enabled: false              # off by default — opt-in surface
  schedule: "0 8 * * *"       # cron expression
  max_items: 5
  surfaces: [cli_banner]      # list — picks active presenters
  quiet_hours: [22, 7]        # 24h range, no surfacing inside
```

### Server-side requirement

OpenHub adds a distinct `GET /v1/feed` endpoint — broad relevance against a tag profile rather than narrow intent match. See [`openhub-mvp.md` §15](./openhub-mvp.md#15-v11--feed-endpoint-deferred) for the spec.

### Open design questions (answer when v1.1 starts)

1. **Surface preference** — most users want which? Try the cheapest (CLI banner on session start) first, layer push surfaces only if users ask.
2. **Volume** — 3 items/morning? 10? Per-surface?
3. **Dedup placement** — server-side (`feed_views` table tracks `(submitter_hash, trace_id)`) or client-side (agent maintains a local seen-set, sends `?exclude=...`)? Trade-off: server state vs query size.
4. **Quiet hours** — respect user's local time; how to detect timezone reliably (env var? OS query?)
5. **Tag profile staleness** — how often does the agent re-derive its tag profile? Per-task (today, in v1) or aggregated daily?
6. **Cold-start** — a brand-new agent with no tag profile yet: skip feed entirely until N tasks have run, or send a generic onboarding feed?

### When to start

After v1 has shipped and you've used it for ~2 weeks. The flywheel argument from HANDOVER hinges on this surface eventually existing — but it doesn't need to ship simultaneously with v1.

---

## Appendix A — Comparison of novelty rules (full reasoning)

| Option | Cost/session | Coverage of "valuable emit" | Failure mode |
|---|---|---|---|
| **(a) Simple binary** — emit iff query returned nothing | $0 | ~60% | Network stagnates — no improvement signal flows in once an intent has any trace |
| **(b) Signals-only proxy** — compare `loop_count`, `token_cost` to trace's claimed values | $0 | Noisy — high false-positive | Self-report vs self-report; tool count balloons for unrelated reasons (retries, follow-ups, confirmation steps) |
| **(c) LLM judge always** — Haiku scores `is_novel` every session | ~$0.005 | Broad | Pays Haiku on every session including ones with no trace to compare against |
| **(d) Binary + LLM judge when trace was used (CHOSEN)** | $0 (early), trending to ~$0.005 (full network) | Matches (c) where it matters, free elsewhere | Slightly more code than (c) — two emission paths |

Rule (d) is what we're shipping. (a) was considered as a v0.1 fallback if (d) implementation runs long.

---

## Appendix B — Glossary

- **TraceCard** — the structured wire format for a trace. Frozen schema in `plugin_sdk/traces.py`.
- **OpenHub** — the network this plugin talks to. Separate repo. See `openhub-mvp.md`.
- **submitter_hash** — opaque per-agent stable id. Never user identity. Used by network for rate-limiting + future trust scoring.
- **distilled_insight** — the LLM-written one-paragraph summary other agents will read.
- **Novelty judge** — Haiku call that decides whether an agent improved on a trace it used. Only fires on the `trace_used != None` branch.
- **Outbox** — local queue of pending submissions. Drained when network is back.
- **Curation engine** — server-side scorer (lives on OpenHub, not here). We just consume its top-K output.
- **Stage 1 / 2 / 3** — local Mac → Pi+ngrok → real server. See openhub-mvp.md.

---

## Appendix C — Cross-system pickup checklist

If you're returning to this from a different machine:

1. `git clone https://github.com/<your-fork>/opencomputer.git`
2. Read this doc top-to-bottom
3. Read `~/Downloads/HANDOVER.md` (or wherever you saved it on the new system)
4. Read [`openhub-mvp.md`](./openhub-mvp.md)
5. Check `git log --oneline -- extensions/social-traces/ plugin_sdk/traces.py plugin_sdk/hooks.py` — what's already done?
6. Cross-reference completed commits against `§10 Implementation phases` checkboxes
7. Pick the next un-checked phase
