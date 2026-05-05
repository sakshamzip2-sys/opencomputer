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
  Post-task hook reads runtime.custom["trace_used"]
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

### 7.2 Runtime flag (pre→post signal)

Set by `prefetch.on_before_task()`:

```python
runtime.custom["trace_used"] = "<trace_id>"  # or None if no trace was used
runtime.custom["trace_used_card"] = trace_card  # full card, for novelty judge
```

Read by `subscriber._run_pipeline()` to decide which path to take.

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

### Phase 0 — SDK additions (1-2 hours)

- [ ] Add `BEFORE_TASK` to `plugin_sdk/hooks.py:HookEvent` and `ALL_HOOK_EVENTS`
- [ ] Create `plugin_sdk/traces.py` with `TraceCard`, `TraceMeta`, `TraceStep`, `TraceNetworkClient` ABC, `SubmitReceipt`, `QueryResult`, `TRACE_API_V1`
- [ ] Export all new names from `plugin_sdk/__init__.py`
- [ ] Add `tests/test_plugin_sdk_traces.py` — schema serialization round-trip, ABC enforcement
- [ ] Confirm SDK boundary test still passes (`tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer`)

### Phase 1 — Loop integration (1 hour)

- [ ] Wire `BEFORE_TASK` fire in `opencomputer/agent/loop.py` (location: after USER_PROMPT_SUBMIT, before prompt build)
- [ ] Test: a registered hook returning `decision="rewrite", modified_message="..."` causes a `<system-reminder>` user message to land in `messages`
- [ ] Test: existing `run_conversation` flow unaffected when no `BEFORE_TASK` hook is registered

### Phase 2 — Plugin scaffold (2-3 hours)

- [ ] `opencomputer plugin new social-traces --kind mixed` (uses Sub-project B scaffolder)
- [ ] Hand-edit `plugin.json` with correct fields (kind=mixed, enabled_by_default=false initially)
- [ ] Create `config.py`, `identity.py` — boilerplate
- [ ] Wire empty `prefetch.py` and `subscriber.py` so registration works
- [ ] `plugin.py:register(api)` — register the BEFORE_TASK hook + subscriber lifecycle
- [ ] Smoke test: `opencomputer plugins` lists `social-traces`; `oc social-traces enable` flips state file

### Phase 3 — Local-file backend (2-3 hours)

- [ ] `client/local_file.py` — implements `TraceNetworkClient` over `<profile_home>/traces/{inbox,outbox}/` JSON files
  - `query()`: scans `inbox/*.json`, filters by tag overlap, returns top-K
  - `submit()`: writes to `outbox/<uuid>.json`
  - `health()`: returns True if directory writable
- [ ] CLI helper `oc traces inbox add <path>` to seed test traces
- [ ] CLI helper `oc traces inbox list/show/remove`
- [ ] Tests: query with tag overlap returns expected, submit appends file

### Phase 4 — Pre-task hook (3-4 hours)

- [ ] `tag_extractor.py:extract_tags_from_message()` — first cut: simple keyword extraction (LLM upgrade in Phase 8)
- [ ] `prefetch.py:build_query()` — extract intent (use user message verbatim for v0) + tags
- [ ] `prefetch.py:score_traces()` — relevance threshold gate
- [ ] `prefetch.py:format_injection()` — `<trace intent="..." outcome="..." tags="...">...</trace>`
- [ ] `prefetch.py:on_before_task()` — calls client.query() with 1s soft timeout, sets `runtime.custom["trace_used"]`, returns `HookDecision`
- [ ] Integration test: seed trace in inbox, run agent, observe injection in messages

### Phase 5 — Post-task subscriber (3-4 hours)

- [ ] `subscriber.py:EmissionSubscriber` — port from `extensions/skill-evolution/subscriber.py` shape
- [ ] State file: `<profile_home>/traces/state.json` with `{"enabled": bool}`
- [ ] Heartbeat: `<profile_home>/traces/heartbeat`
- [ ] Decision tree at top of `_run_pipeline`:
  - if `trace_used is None`: continue to redact + distill + submit
  - if `trace_used is set`: run novelty_judge first
- [ ] CLI: `oc traces enable/disable/status`

### Phase 6 — Novelty judge (2-3 hours)

- [ ] `novelty_judge.py:judge_novelty_async()` — Haiku call with prompt:
  - inputs: user message, agent transcript, trace card that was used
  - outputs: `is_novel: bool` + `reason: str`
  - cost-guarded via existing `CostGuard` (see skill-evolution wiring)
- [ ] Tests: mock provider returns is_novel=true → emission proceeds; is_novel=false → silent

### Phase 7 — Redactor + distiller (3-4 hours)

- [ ] `redactor.py` — port regex set from `skill_extractor.py` (PII, CC, SSN, paths)
- [ ] `distiller.py` — three-Haiku flow (intent / steps / insight) cost-guarded; returns `TraceCard or None`
- [ ] Apply redactor to BOTH input prompts AND output content (defense in depth)
- [ ] Tests: redact known PII patterns; distill from synthetic transcript yields valid TraceCard

### Phase 8 — LLM tag extractor (2-3 hours)

- [ ] Replace keyword tag extraction with one Haiku call
- [ ] Cache tag extraction per session (don't re-run mid-session)
- [ ] Maintain `tag_profile` accumulator on disk so `prefetch.build_query()` can include profile-bias tags

### Phase 9 — HTTP client + outbox (2-3 hours)

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
