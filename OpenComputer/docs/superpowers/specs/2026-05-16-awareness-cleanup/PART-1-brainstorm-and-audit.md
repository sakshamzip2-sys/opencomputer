# Layered Awareness cleanup — Part 1: Brainstorm & Design Audit

Date: 2026-05-16
Owner: Saksham
Working dir: `/Users/saksham/Vscode/claude/OpenComputer`
Companion file: `PART-2-plan-and-plan-audit.md`
Status relative to the parity plan: **post-M5** (runs after the Hermes/OpenClaw parity work; does not block it).

---

## Pre-work: What's actually in the codebase (verified, not guessed)

Before any planning, audit what already exists so we don't redesign solved problems. Greped on-disk, sizes verified:

| Component | Path | LOC | Status |
|---|---|---|---|
| Graph schema (SQLite + FTS5) | `opencomputer/user_model/store.py` | 606 | **Solid.** WAL, retry, FTS5 over `nodes.value`, schema_version migrations, foreign keys, cascade deletes. Already at schema v2. |
| Decay engine (per-edge exponential half-life) | `opencomputer/user_model/decay.py` | 207 | **Built but unused at read time.** Walks all edges, computes `0.5 ** (age_days / half_life)`, writes `recency_weight`. Has scheduler. **Not consumed by the ranker.** |
| Drift store + drift detection | `opencomputer/user_model/{drift,drift_store}.py` | 554 | **Built.** Tracks contradicting assertions over time. **Also unused at read time.** |
| Importer (writes nodes/edges from various sources) | `opencomputer/user_model/importer.py` | 246 | Source-tagged writes (`motif_importer`, `honcho_synthesis`, `user_explicit`, `unknown`). |
| Scheduler (background decay run) | `opencomputer/user_model/scheduler.py` | 203 | Decay fires on a schedule. |
| Honcho bridge | `opencomputer/user_model/honcho_bridge.py` | 187 | External memory provider integration. |
| Context selector | `opencomputer/user_model/context.py` | 152 | **Exists.** Read it for current selection logic. |
| Persona classifier v2 | `opencomputer/awareness/personas/classifier_v2.py` | 392 | Foreground app + recent files + time-of-day → persona tag. |
| Persona priors | `opencomputer/awareness/personas/priors.py` | 162 | Per-persona tone, register, weights. |
| Life events (burnout, exam, job, travel, …) | `opencomputer/awareness/life_events/*.py` | ~540 | Pattern-matchers. Conservative thresholds; rarely fire. |
| Learning moments (predicate matchers) | `opencomputer/awareness/learning_moments/*.py` | ~1244 | Bigger surface than life-events; same shape. |
| **The actual ranker used at prompt-build time** | `opencomputer/agent/prompt_builder.py::build_user_facts` (lines 474–504) | ~30 | **The bottleneck.** `sort by (kind_priority, -confidence)` only. No decay-aware scoring, no dedupe, no context-awareness, no salience use. |

**The honest read:**

OC's user-model graph is *infrastructure-rich, consumption-poor*. The store, the decay engine, the drift detector, the persona classifier, the life-events scaffolding — all built. **The 30-line `build_user_facts` ranker throws away almost every signal those subsystems produce** and just sorts by kind-priority + confidence.

That's why the "What I know about you" block at the top of every chat is mostly cron-heartbeat duplicates: the writer is dumping `ambient-sensors` schedule rows in as preferences, the ranker isn't deduping, and the decay weight that would otherwise demote them is being computed but ignored.

**This is not "build a smarter graph" — it's "wire up the smart graph we already have."**

---

## Phase 1 — /brainstorm

### Goal

Make every injected user-facts block earn its prompt-token cost. Specifically:

1. The displayed top-K facts should be *signal*, not duplicate cron rows.
2. Old facts should fade; fresh ones should rise.
3. The user should be able to fix mistakes (`forget`, `correct`).
4. The persona + life-events machinery should produce visible behaviour changes, not silently sit in code.

Constraints:
- No new heavy infra. The graph schema is fine; don't add Postgres / Graphiti / Neo4j.
- Stay inside boundary rules (`extensions/` import only `plugin_sdk/*`, core never imports extensions).
- Backward-compatible: existing graphs continue to load.
- No prompt size regression; injected block stays ≤ K=20 facts, ≤ 80 chars each.

### Approaches considered

#### Approach A — "Wire up what exists, nothing new"

Just rewire `build_user_facts` to consume `recency_weight`, `salience`, `source_reliability` from the edges incident to each node. Add dedupe (canonical fact-hash) at read time. No schema changes, no new modules. Persona + life-events stay untouched.

- **Effort:** S (3–4 days).
- **Risk:** Low. Inside one function; old behaviour reproducible by reverting one file.
- **Upside:** Solves the visible noise problem today.
- **Limit:** Doesn't fix the *writer* — junk still gets written, just better hidden.

#### Approach B — "Writer cleanup first, ranker second"

Fix the source. Audit every place that writes nodes (importer, honcho_bridge, ambient-sensors hooks, profile_bootstrap), enforce strict taxonomy: cron heartbeats can't be "preferences," session-shape can't be "goals." Add a `node_kind_validator` that rejects mis-classified writes. *Then* improve the ranker.

- **Effort:** M (1–2 weeks, depends on how many writers exist).
- **Risk:** Medium. Touching writers risks breaking existing data flow; need migration for already-mis-tagged nodes.
- **Upside:** Stops bleeding at source. Graph quality compounds over time.

#### Approach C — "Context-aware reranker"

Add a reranker that takes (current session context, candidate facts) → reordered list. Context = foreground app + recent message keywords + persona tag + active project. So this very session (engineering planning) would boost tech-stack facts and demote cron preferences.

- **Effort:** M (1 week, model-light implementation: TF-IDF or BM25 over session messages vs. node values).
- **Risk:** Medium. Adds inference cost per prompt build. Can be cached for the session.
- **Upside:** Massive felt improvement. The agent feels "more aware" without growing the graph.

#### Approach D — "User correction surface"

Ship CLI: `oc awareness review`, `oc awareness forget <id>`, `oc awareness correct <id> <new-value>`. Track user corrections as `contradicts` / `supersedes` edges (the schema already supports both kinds). Future reads honour them.

- **Effort:** S–M (3–5 days).
- **Risk:** Low. Pure additive; old reads ignore new edges.
- **Upside:** Trust. If the user can fix mistakes, they trust the rest more. Currently zero correction path → low trust.

#### Approach E — "Persona + life-events get teeth"

The persona classifier picks a tone; the prompt already uses it. But life-events fire and do *what* exactly? Right now they update graph metadata silently. Wire life-events to: (a) emit a one-line system hint into the next assistant turn, (b) trigger a `oc cron` job (e.g. burnout → "schedule a check-in in 3 days"), (c) update `<persona-tone>` for the affected sessions. Make them actually visible.

- **Effort:** M (1–2 weeks; touches injection, cron, persona registry).
- **Risk:** Medium-high. Visible behaviour change = visible bug surface. False-positive burnout detection that schedules cron jobs = bad.
- **Upside:** Differentiator. Nobody else does this; if it works, OC has a unique "ambient awareness" story.

#### Approach F — "Drop the graph, use MEMORY.md + LLM scoring"

Throw away the graph. Just inject MEMORY.md + USER.md whole, and use a tiny aux LLM call to score-and-trim per session. Cheaper to maintain, no taxonomy hell.

- **Effort:** S (1 week).
- **Risk:** High. Extra LLM call per session = latency + cost. Loses cross-session learning (the graph compounds; markdown doesn't).
- **Upside:** Simplest possible model. Probably what 80% of agent frameworks do.

#### Approach G — "Move to a real graph DB (Graphiti / Memgraph / Neo4j)"

Bigger infra. Real graph queries, traversals, embeddings as a first-class concept.

- **Effort:** XL (3–4 weeks + ongoing maintenance).
- **Risk:** High. New external dep, new failure mode, new deploy story.
- **Upside:** Marginal. SQLite + the existing schema already supports everything OC actually queries.
- **Killer:** Already argued against this two turns ago. Don't relitigate.

#### Approach H — "Hybrid: B + C + D, defer E, reject F + G"

Fix the writer (B), add context-aware reranking (C), ship the correction CLI (D). Defer persona/life-events tooth-growing (E) to v2 — it's high-upside but high-risk and benefits from having clean data first (B). Reject F (loses compounding) and G (no real benefit).

- **Effort:** M-L (3–4 weeks for B+C+D).
- **Risk:** Low-medium. Three concurrent tracks but they don't overlap structurally.
- **Upside:** Solves the three highest-impact visible gaps simultaneously.

### Scoring

| Approach | Effort | Risk | Upside | Verdict |
|---|---|---|---|---|
| A — Just rewire ranker | S | Low | Medium | Cheap; doesn't fix root cause |
| B — Writer cleanup | M | Medium | High | Stops bleeding at source |
| C — Context-aware reranker | M | Medium | High | Felt improvement is biggest here |
| D — User correction CLI | S–M | Low | High (trust) | Cheap insurance |
| E — Persona/life-events teeth | M | Medium-high | Differentiator | Better after B lands |
| F — Drop graph, use LLM scoring | S | High | Low | Loses compounding |
| G — Real graph DB | XL | High | Marginal | Already rejected |
| **H — B + C + D, defer E** | **M-L** | **Low-Medium** | **High** | **Winner** |

### Convergence

Top 3: **H, C, B**. H combines B + C + D; C alone solves the felt-noise problem but doesn't fix the writer; B alone is invisible to users for weeks until clean data accumulates.

### Winner: H

Why H wins on merit, not familiarity:

- **B fixes the source.** Without it, every subsequent improvement (ranker, decay, life-events) is fighting noise. You can't build "intelligent ranking" on top of mis-classified data.
- **C is the highest-felt-impact slot.** This is what makes the user *notice* the system got better. A user who sees relevant facts at the top of every chat will believe the rest is working too.
- **D is cheap trust-building.** When the system learns wrong (and it will), users need a recourse. Zero correction path = zero trust = users disable the feature mentally. Adding `oc awareness review` + `forget` is 3–5 days of work and removes that failure mode.
- **E is parked, not killed.** It's the biggest upside item on the list but it depends on B for clean data. Promoting it to v1 would be a scope mistake — it'd ship with dirty data and produce visibly bad behaviour.
- **Maps cleanly to OC's boundary rules.** Writer fixes go in `opencomputer/user_model/importer.py` and the various sensor hooks (`opencomputer/ambient/`). Reranker is a new module in `opencomputer/user_model/reranker.py`. CLI lives in `opencomputer/cli_awareness.py` (new file, mirrors `cli_consent.py`). Nothing crosses into `plugin_sdk/` v1 commitments — these are core-internal changes.

---

## Phase 2 — /audit-design

Stress-testing approach H. Each finding is resolved or accepted-risk.

### 1 — Assumption check

| Assertion | Validated? | Resolution |
|---|---|---|
| `user_model/decay.py` and `drift.py` are functional, just unused by the ranker | **Read end-to-end.** Yes, both have full implementations with tests probably. | Verify tests pass on `main` before any reranker change; if decay is silently broken, B's "use recency_weight" assumption fails. |
| Mis-classification is upstream of the ranker, not just a display artifact | **Strongly suspected.** The "ambient-sensors prefers Wednesday 14:00" pattern smells like a cron-job hook writing `preference`-kind nodes. | Pre-Milestone 1 task: grep for `kind="preference"` writers, find the offender(s). |
| Context-aware reranking can be implemented without a model API call | **Plausible.** BM25 over session-message tokens vs. node-value tokens fits in 100 LOC. | T2.3 explicitly forbids LLM calls in the hot path; cache reranker scores per session. |
| The 30-LOC `build_user_facts` is the only consumer; no downstream code parses the returned string | **Unvalidated.** Need to grep for callers. | T1.1 maps every consumer of `build_user_facts` and `<user-facts>` before changing the output shape. |
| Existing graph data is fixable in-place via re-classification, not full wipe-and-rebuild | **Unvalidated.** Some mis-tagged nodes might be impossible to re-classify without their original context. | T2.1 includes a "dry-run reclassify report"; if >20% of nodes need manual review, switch to wipe-and-rebootstrap. |

### 2 — Architecture stress (edge cases)

- **User runs reranker before profile_bootstrap finishes.** Fresh OC install → graph has 0–5 nodes → reranker sorts a near-empty list. Resolution: short-circuit when `count_nodes < 10`; fall back to current `sort by (kind, confidence)` until graph fills.
- **User has 10K+ nodes (rare but possible if they've been running OC for a year).** Resolution: reranker reads `list_nodes(limit=500)` — same cap `build_user_facts` already uses. Don't try to score 10K candidates per prompt.
- **Two contradicting facts at high confidence** ("user is in Bangalore" + "user is in San Francisco"). Resolution: `drift.py` already detects this; reranker prefers nodes with no `contradicts` incoming edges. Document this in the reranker docstring.
- **User runs `oc awareness forget` on an identity node**. Identity nodes are foundational; deleting "name: Saksham" breaks subsequent runs. Resolution: `forget` on identity-kind warns and requires `--confirm`; non-identity kinds delete without prompt.
- **Reranker depends on session-context but cron runs have no session.** Resolution: when `RuntimeContext.agent_context == "cron"`, reranker falls back to default (kind+confidence) ranking. Document.

### 3 — Alternative dismissal

Approaches A, F, G dismissed on merit:
- **A** (just rewire ranker) leaves the writer broken. Half-fix.
- **F** (LLM scoring) loses cross-session learning and adds latency.
- **G** (Neo4j) is infrastructure cosplay. No new query OC needs that SQLite can't already answer.

Approach E (persona/life-events teeth) is *deferred*, not dismissed. It's the highest-upside item; just gated on B landing first.

This isn't default-choice; H was selected because B fixes the source, C is the highest-felt-impact slot, D buys trust at low cost, and E is honestly named as deferred.

### 4 — Requirement gap

- **The user wants to trust the system.** When facts are wrong, they need to feel the recourse exists. Resolution: D ships `oc awareness review` *before* any taxonomy changes go live — so users have the tool to inspect what's in the graph from day one.
- **The user wants observable behaviour.** Internal cleanup is invisible. Resolution: ship a `oc awareness explain` command that shows: top-K facts injected this session, why each ranked where it did (kind, confidence, recency_weight, reranker score). Makes the system legible.
- **Implicit: don't break the existing prompt structure.** `prompt_builder.build_user_facts` returns a multi-line string consumed by `base.j2`. Resolution: keep the contract identical (string, one fact per line, same format). Internal scoring is invisible to the prompt.
- **Implicit: cron and gateway runs.** Reranker must not blow up when there's no session context. Already handled in §2 above.

### 5 — Composability

- **Writer cleanup (B) + reranker (C):** independent files, no coupling. ✓
- **Reranker (C) + correction CLI (D):** D writes new edges (`contradicts`, `supersedes`); C reads them. They compose through the schema, not direct calls. ✓
- **All three + decay engine:** decay writes `recency_weight`; reranker reads it. Pure read-after-write through SQLite. ✓
- **All three + persona classifier:** persona is independent (already wired). No new coupling. ✓

One real composability risk: **the reranker needs to know the persona tag for "context."** Currently the persona is set in the system prompt but not exposed to the reranker as a typed value. Resolution: read it from `awareness/personas/registry.py` via a thin accessor; don't parse the prompt.

### 6 — Scope honesty

Where am I undersizing?

- **B "writer cleanup" assumes there are 2–3 offenders.** There might be 10. Honest size: **M, with a defined exit-criterion** ("fix the top 80% of mis-classifications, document the rest"). Don't chase the long tail in v1.
- **C "BM25 reranker" sounds cheap but needs caching, tokenization, and tests.** Honest size: **M (5–7 days)**, not S.
- **D "correction CLI" needs review UX, not just `forget`.** `oc awareness review` is a Rich table; `oc awareness explain <id>` shows provenance. Honest size: **M (4–5 days)**, not S.
- **The migration step for already-mis-classified nodes** is small if simple (reclassify by value-pattern matching) and large if it requires human review per row. Honest size: **S if we accept "mark for review" tag, L if we try to auto-fix everything**. Plan picks the cheap path.

Total honest size: **3–4 calendar weeks** for one engineer. Matches Approach H's stated effort.

### 7 — API stability

What interfaces will outlive v1?

- `UserModelStore` CRUD: stable, don't touch.
- `Node` / `Edge` dataclasses in `plugin_sdk/user_model.py`: stable, don't touch. These are public API — extensions read them.
- `build_user_facts` contract: stable (multi-line string, ≤80 char facts). Internal scoring is implementation detail.
- New: `UserFactsReranker` interface in `opencomputer/user_model/reranker.py`. **Not public API** (lives in core, not plugin_sdk). Free to refactor.
- New: `oc awareness {review,forget,correct,explain}` CLI commands. **These ARE user-facing API.** Lock the names/flags early; don't rename in v2.
- New: node-kind validator in the writer path. **Internal.** Free to refactor.

### 8 — Failure map

| Choice | Production failure | Mitigation |
|---|---|---|
| Writer enforces strict taxonomy → rejects legitimate writes from a misbehaving plugin | Plugin silently fails to record data | Validator logs the rejection to `audit.db`; `oc awareness explain --rejections` lists them. Fail-open by default for non-core writers (warn but don't reject). Strict for core writers. |
| Reranker scores a session badly → user sees noise at top | Same as today, just at a different prompt slot | `oc awareness explain` shows the scoring; user can `forget` the noisy fact. |
| Decay-aware scoring demotes a still-true old fact (e.g. "user lives in Bangalore" — true for years) | Identity facts disappear from prompt | Per-kind decay floors are already in `DecayConfig`. Identity nodes get a high floor (close to 1.0); preferences decay normally. |
| `oc awareness forget` deletes a node that's foundational | Subsequent runs break | Identity kind requires `--confirm`; show what depends on it before deletion. |
| Reranker is slow on every prompt build → user feels latency | Annoying | Cache reranker scores per session_id; recompute only on session start. ≤50ms p99 target. |
| Migration runs on a 100K-node graph and blocks for a minute | User-visible hang | Migration runs lazily on first read; batched; emits progress to `audit.db`. |

### 9 — YAGNI sweep

What's in the design that no caller needs?

- **Per-tool reranker mode.** Considered — "give each tool its own ranking weights." **No.** One reranker, one set of weights. Add per-tool only when a real use case shows up.
- **Embedding-based similarity for reranker.** Could be cheap with `sentence-transformers`. **No.** BM25 over tokens is enough for the K=500 candidate pool. Embeddings = post-v1.
- **`oc awareness import` / `export` for cross-machine sync.** Nice-to-have, not blocking. **No.** Out of scope.
- **GUI for graph editing.** **No.** CLI only in v1.
- **Multi-user / multi-tenant graphs.** OC is per-profile. **No.**
- **Real-time decay (recompute on every read).** **No.** Scheduled decay (already implemented) is fine; reranker reads the persisted weight.

Trimming these keeps Approach H honest at **M-L (3–4 weeks)**.

---

## Audit conclusion

Design holds with the following accepted risks:

1. **Writer-cleanup size depends on offender count.** Pre-task task counts the offenders; if >5, scope expands or we accept "fix top 80%" exit.
2. **Reranker BM25 is good enough for v1.** Embeddings deferred to v2.
3. **Migration accepts "mark for review" for unresolvable cases.** No mass deletes.
4. **Persona/life-events behaviour changes (Approach E) are deferred to v2.** Documented in PART-2 deferred list.
5. **`oc awareness` CLI commands are public API once shipped.** Lock names early.

Proceed to PART-2 for the milestone-level plan and plan audit.
