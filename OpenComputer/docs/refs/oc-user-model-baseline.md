# OC user-model graph — baseline (what's wired, what's dormant)

Date: 2026-05-16 · Produced by: awareness-cleanup T1.1 · Method: every file in
`opencomputer/user_model/` + `prompt_builder.build_user_facts` + `plugin_sdk/user_model.py`
read end-to-end; consumers grepped.

**This doc is the GATE for Milestones 2/3/4.** If a claim here is wrong, those
estimates shift. Line numbers are as of branch `feat/awareness-cleanup-2026-05-16`.

---

## 1. Component map (verified on-disk)

| Component | Path | LOC | Status |
|---|---|---|---|
| Graph store (SQLite + FTS5) | `user_model/store.py` | 606 | **Wired.** Schema v2, WAL, retry-on-busy, FTS5 over `nodes.value`, cascade deletes. |
| Decay engine (per-edge-kind exp. half-life) | `user_model/decay.py` | 207 | **Functional, tested — invoked only by CLI.** Writes `recency_weight`. |
| Decay/drift scheduler | `user_model/scheduler.py` | 203 | **DORMANT.** `DecayDriftScheduler` is never instantiated outside tests (grep: 0 hits in `opencomputer/`). |
| Drift detector (symmetrized KL over motifs) | `user_model/drift.py` | 254 | **Functional, tested — invoked only by CLI.** See §5 caveat. |
| Drift report archive | `user_model/drift_store.py` | 300 | **Wired** when a `DriftStore` is passed to `DriftDetector`. |
| Motif → graph importer | `user_model/importer.py` | 246 | **Wired.** Source-tagged writes (`source="motif_importer"`). |
| Honcho bridge | `user_model/honcho_bridge.py` | 187 | Partial — real Honcho provider deferred; protocol + mock path live. |
| Context ranker (4-factor edge score) | `user_model/context.py` | 152 | **Functional — but NOT on the prompt path.** See §4. |
| Prompt-time ranker | `agent/prompt_builder.py::build_user_facts` (470–503) | ~30 | **The bottleneck.** `sort by (kind_priority, -confidence)` only. |

PART-1's component sizes were accurate. PART-1 **missed two things**, corrected below: a
working `ContextRanker` already exists (§4), and the decay scheduler is not merely
unscheduled but never wired at all (§5).

---

## 2. Data flow

```
 WRITERS                          STORE                      READERS
 ───────                          ─────                      ───────
 MotifImporter ──upsert_node──┐                          ┌── build_user_facts()  → base.j2  <user-facts>
   (importer.py)              │                          │     (agent/loop.py:2134, ONLY caller)
 F4HonchoBridge ──────────────┼──> UserModelStore ────────┤
   (honcho_bridge.py)         │    graph.sqlite           ├── ContextRanker.rank()
 profile_bootstrap ───────────┘    nodes + edges          │     (cli_user_model.py `context` cmd ONLY)
   (write_interview_answers_to_graph)                     │
 DecayEngine ──update_edge_recency_weight──> edges        └── search_nodes_fts() (CLI search)
```

Validated assumptions (PART-2 §4.1):
- **`build_user_facts` has exactly one caller** — `agent/loop.py:2134`, wrapped in
  try/except → degrades to `""`. Changing its *internals* is safe; its *contract*
  (returns a newline-joined string, one fact per line, ≤80 chars) must hold.
- **`<user-facts>` has exactly one template consumer** — `agent/prompts/base.j2:204-207`,
  gated by `{% if user_facts %}`.

---

## 3. Schema (store.py, `SCHEMA_VERSION = 2`)

- **`nodes`** — `node_id` (TEXT PK, uuid4), `kind`, `value`, `created_at`, `last_seen_at`,
  `confidence`, `metadata_json`. FTS5 shadow `nodes_fts` over `value`, auto-synced by triggers.
- **`edges`** — `edge_id`, `kind`, `from_node`, `to_node`, `salience`, `confidence`,
  `recency_weight`, `source_reliability`, `decay_rate`, `created_at`, `evidence_json`,
  `source` (v2 column). FK `from/to_node → nodes` **`ON DELETE CASCADE`**.
- **Migrations** — `MIGRATIONS` dict keyed `(from, to)`, `apply_migrations` advances to
  `SCHEMA_VERSION`. Adding v3 = add `(2,3)` entry + `_migrate_v2_to_v3` + bump constant.
- `NodeKind = identity | attribute | relationship | goal | preference` (frozen Literal).
- `EdgeKind = asserts | contradicts | supersedes | derives_from` (frozen Literal).
- DB path: `<profile_home>/user_model/graph.sqlite` via `_default_db_path()`.

`delete_node` is a **hard** delete (cascades edges). No soft-delete primitive exists —
M1 `forget` will add one via a `metadata` flag (no schema change; see PART-2 §4.2).

---

## 4. KEY FINDING — two rankers, the prompt path uses the weaker one

There are **two** ranking implementations:

1. **`ContextRanker`** (`context.py`) — scores each node by the max over its incident
   edges of `salience × confidence × recency_weight × source_reliability`; orphan nodes
   fall back to `confidence × 0.5`. **This already consumes `recency_weight`** — i.e. it
   is already decay-aware. Tie-break by `last_seen_at`. Token-budget aware.
   *Consumer: `oc user-model context` CLI only.*

2. **`build_user_facts`** (`prompt_builder.py`) — `sorted(nodes, key=(kind_order, -confidence))`.
   No edges, no `recency_weight`, no dedupe, no salience. *Consumer: the prompt path.*

**The prompt the user sees every chat is built by ranker #2; the good ranker #1 is
reachable only from a dev CLI command.** This reframes the milestones:

- M3's `UserFactsReranker` is **not** "build a ranker from scratch" — `ContextRanker`
  already does edge-aggregate + recency scoring. M3 adds (a) kind-priority, (b) BM25 vs.
  session context, (c) caching. **Decide at M3:** extend `ContextRanker` vs. new module.
  PART-2 T3.1 says new module; the overlap is real and worth a deliberate call.
- M4's T4.1/T4.2 accessors (`node_recency_score`, `node_drift_score`) partially duplicate
  `ContextRanker._edge_score` / `_incident_edges`. Reuse, don't re-derive.

---

## 5. Dormant subsystems — detail

**Decay scheduler — fully dormant.** `DecayDriftScheduler` (scheduler.py) is a bus
subscriber that would fire decay every 24h. `grep "DecayDriftScheduler("` across
`opencomputer/` returns **zero non-test hits**. Nothing constructs it, nothing calls
`attach_to_bus()`. Consequence: `recency_weight` is **only** ever recomputed when a user
manually runs `oc user-model decay run`. On a normal install, edge `recency_weight`
stays at its insert-time `1.0` forever. **M4 T4.4 must wire the scheduler** (attach in
the agent loop / gateway startup) — this is more than "add a cron entry"; the scheduler
object itself has no instantiation site.

**Drift detector — works, but not over `contradicts` edges.** `DriftDetector` computes
symmetrized KL divergence over **motif distributions** (recent window vs. lifetime),
keyed on `"{motif.kind}/{first_token}"`. It does **not** read or write graph
`contradicts` edges. The `contradicts` `EdgeKind` exists in the schema but
`importer.py` explicitly never emits it ("CONTRADICTS edges are not auto-emitted here").
So today **nothing writes `contradicts` edges.** M1's `oc awareness correct` will be the
first writer (via `supersedes`, and optionally `contradicts`). M4's `drift_penalty`
(count of incoming `contradicts`) is a no-op until M1 ships the correction path — they
compose, but M4 depends on M1 for any signal.

---

## 6. CLI surface that already exists (don't rebuild)

- `oc awareness {patterns,personas} {list,mute,unmute}` — life-event + persona controls
  (`cli_awareness.py`, **already wired** into `oc`). M1 EXTENDS this group.
- `oc user-model nodes {list,add}`, `oc user-model edges list`, `oc user-model search`,
  `oc user-model import-motifs`, `oc user-model context`, `oc user-model decay run`,
  `oc user-model drift {detect,list,show}` (`cli_user_model.py`).

**Overlap to be aware of:** `oc user-model nodes list` already lists nodes. The plan's
`oc awareness review` is justified as a *distinct* surface — it is provenance- and
ranking-focused (source, incoming-`contradicts` count, the lens the *user* uses to
decide "is the agent wrong about me"), where `user-model` is dev-facing raw CRUD.
`cli_user_model.py` (`nodes_list`, `context`) is the best style template for M1's
table rendering — same domain, same store.

---

## 7. Implications for the milestones

| Milestone | Baseline impact |
|---|---|
| M1 (CLI) | `cli_awareness.py` exists — extend, don't create. Mirror `cli_user_model.py` table style. `forget` soft-delete = `metadata` flag (no schema change). |
| M2 (writer cleanup) | Node writers to enumerate in T1.2: `MotifImporter` (3 motif kinds), `F4HonchoBridge`, `profile_bootstrap.write_interview_answers_to_graph`. All already source-tag *edges*; `nodes` carry no `source` — validator works on `(kind, value)`. |
| M3 (reranker) | `ContextRanker` already does decay-aware edge scoring. Decide: extend vs. new module. |
| M4 (decay+drift) | Decay scheduler is *unwired*, not just unscheduled — T4.4 is a real wiring task. `contradicts` edges have no writer until M1's `correct` lands. |

Baseline holds. Proceed to T1.2.
