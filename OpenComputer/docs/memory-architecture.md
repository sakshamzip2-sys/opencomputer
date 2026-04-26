# Memory architecture — F4 user-model graph + Honcho synthesis (the hybrid)

> **Honest framing.** This document is the answer to a real user question: "is
> our F4 user-model graph + Honcho fallback better, equal, or worse than what
> Hermes does with Honcho-deepening?" We dropped the marketing copy and wrote
> the actual trade-off here. If you came looking for a glowing pitch, scroll
> elsewhere.

## TL;DR

- **F4 alone is technically *inferior* for the goal of "deepening user
  understanding."** It's a static, deterministic, rule-driven graph. It cannot
  synthesize new hypotheses, cannot adapt between turns, cannot answer "what
  would this user prefer?" beyond restating stored facts.
- **F4 is *superior* for audit / transparency / offline determinism.** Every
  edge has explicit provenance, decay is per-kind configurable, no network
  round-trips, no LLM black-boxes.
- **The right answer is *not* "swap to Honcho."** It is: redefine the contract
  so each memory layer does what it's actually good at, and keep them
  complementary instead of redundant.

## The hybrid contract

```
                 motif inference
                       │
                       ▼
                  ┌─────────┐    one-way feed
                  │   F4    │ ────────────────────► Honcho observations
                  │  graph  │      (motifs only,
                  └─────────┘       skip honcho_*
                       ▲                tagged edges)
                       │
              materialize as low-confidence
              edges, source="honcho_synthesis"
                       │
                       │
                  ┌─────────┐
                  │ Honcho  │
                  │ deriver │
                  └─────────┘
                       │ dialectic claims
                       ▼
                 prefetch context
                  for next turn
```

### Roles

| Layer | Role | Strengths | Weaknesses |
|---|---|---|---|
| **F4 (`opencomputer/user_model/`)** | Audit log of explicit facts. | Deterministic, transparent, offline, fast (~10 ms prefetch), per-kind decay control. | Non-learning; can't synthesize. |
| **Honcho (`extensions/memory-honcho/`)** | Reasoning / synthesis layer. | Theory-of-mind synthesis, latent user representations, dialectic "what would they prefer?" answers. | Network round-trip, opaque, requires Postgres + deriver service. |
| **MemoryBridge (`opencomputer/agent/memory_bridge.py`)** | Orchestration. | Decides which layer to query, mediates the one-way feed F4 → Honcho. | Adds complexity; misconfig = double-write. |

### Cycle prevention

The hybrid would loop if Honcho's synthesis claims fed back as motifs that the
F4 importer re-ingested. Cycle prevention is built into the schema:

- Every edge has a `source` column (Phase 4 of catch-up plan, schema v2).
- Motif importer tags its writes `source="motif_importer"`.
- Honcho-derived edges (when Phase 4.B lands) tag writes `source="honcho_synthesis"`.
- The MemoryBridge feeder skips edges where `source.startswith("honcho_")`
  before pushing observations to Honcho.

This breaks the self-reinforcement loop deterministically — no heuristics,
no time-windowed dedup, no probabilistic fudge. The schema is the gate.

## What ships in Phase 4 (the catch-up plan)

**Done in Phase 4.A (schema groundwork):**

- ✅ `Edge.source: str = "unknown"` field added to `plugin_sdk/user_model.py`
  (backward-compatible default).
- ✅ `edges.source` column + `idx_edges_source` index, schema v1 → v2
  migration in `opencomputer/user_model/store.py`.
- ✅ Motif importer (`opencomputer/user_model/importer.py`) tags all three
  edge constructors with `source="motif_importer"`.
- ✅ Legacy v1 databases auto-migrate to v2 on first open; existing edges
  carry `source='unknown'` forward.

**Deferred to Phase 4.B (live bridge wiring — needs running Honcho in CI):**

- 🔜 `MemoryBridge.sync_turn()` — push tagged motif edges to Honcho as
  observations (one-way, skip `honcho_*`-tagged sources).
- 🔜 `MemoryBridge.prefetch()` — call Honcho dialectic; if confidence ≥ 0.6,
  materialise as a low-confidence F4 edge tagged
  `source="honcho_synthesis"`, with confidence halved (0.6 × 0.5 = 0.3).
- 🔜 Integration test using `extensions/memory-honcho/docker-compose.yml`
  to bring up a real Honcho service in CI.

The Phase 4.B work is straight bridge plumbing on top of the schema that
4.A landed. It's separated because validating it requires a real Honcho
Deriver running, which means adding Docker + CI orchestration. That's
worth doing right, not rushing.

## Mode of operation: which layer answers what?

| Question shape | Layer | Why |
|---|---|---|
| "What time of day does the user start work?" | F4 | Explicit pattern; F4's motif edges hold this with full provenance. |
| "What kind of communication style does the user prefer?" | Honcho | Latent / synthesised; F4 has no such edge until Honcho materializes one. |
| "Why was this fact ranked high in context?" | F4 | Edge provenance + ranker scores explain it deterministically. |
| "Is the user grumpy today?" | Honcho | Dialectic synthesis from recent observations. |
| "Tell me everything you know about this user." | Both — F4 lists explicit facts, Honcho writes the narrative. |

## What we do NOT claim

- **F4 is not "Honcho-quality user understanding."** It's a transparent
  fact graph. Calling it a learning system would be marketing.
- **Honcho is not free.** It needs Postgres + a Deriver worker. If the
  user runs offline, only F4 works — and Honcho's synthesis simply doesn't
  exist for that profile.
- **"Hybrid is better than either alone" is conditional on the user's
  goal.** If the only goal is offline + audit, drop Honcho. If the only
  goal is sophisticated synthesis, drop F4 and accept the operational
  burden of Honcho. The hybrid is right when *both* matter — which is
  the framework's stated identity.

## Comparison to Hermes

Hermes (NousResearch hermes-agent) ships with Honcho as the *only* memory
provider option. They don't have an F4-equivalent: their declarative
memory is plain `MEMORY.md` + `USER.md` with no graph/audit layer. So:

- Hermes wins on synthesis quality (Honcho is doing the work).
- OpenComputer wins on audit / transparency / offline determinism (F4 is
  the moat there).
- The hybrid lets us keep both wins. That is the design choice the user
  asked us to defend honestly — and this is the defense.

## File map

| File | Role |
|---|---|
| `plugin_sdk/user_model.py` | Public Edge / Node / Snapshot dataclasses (now with `source` field). |
| `opencomputer/user_model/store.py` | SQLite-backed CRUD; v2 migration adds `edges.source` + index. |
| `opencomputer/user_model/importer.py` | Motif → edge importer; tags writes `source="motif_importer"`. |
| `opencomputer/user_model/context.py` | ContextRanker — multiplicative four-factor scoring (will accept Honcho-synthesised edges in 4.B). |
| `opencomputer/agent/memory_bridge.py` | Optional Honcho prefetch + sync_turn (4.B will add the cycle-aware feed). |
| `extensions/memory-honcho/` | Honcho self-hosted provider plugin. |
| `tests/test_user_model_source_tagging.py` | Phase 4.A schema + importer tests. |
