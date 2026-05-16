# Layered Awareness — the user-model graph

The agent keeps a **user-model graph** — what it has learned about you: identity
facts, goals, preferences, attributes. The top-ranked facts are injected into
every prompt as the `<user-facts>` block. This directory documents the
2026-05 cleanup of that subsystem.

## Data flow

```
 WRITERS                 STORE                 RANKING               PROMPT
 ───────                 ─────                 ───────               ──────
 profile bootstrap ─┐                     ┌─ UserFactsReranker ─┐
 motif importer ────┼─> UserModelStore ───┤   kind · confidence  ├─> build_user_facts
   (NodeKindValidator│   graph.sqlite      │   · recency · BM25   │   → <user-facts>
    rejects noise)   │   nodes + edges     │   · drift            │
 honcho bridge ──────┘        ▲            └──────────────────────┘
                              │
                    DecayEngine ages edge recency_weight
                    (daily cron tick)
```

- **Writers** add nodes/edges. The motif importer runs every prospective node
  value through `NodeKindValidator` and skips agent-internal noise.
- **The store** (`opencomputer/user_model/store.py`) is SQLite + FTS5.
- **The reranker** (`opencomputer/user_model/reranker.py`) scores each fact by
  a weighted blend and picks the prompt's top-K — excluding soft-deleted and
  `needs_review` facts.
- **`build_user_facts`** renders the block, once per session, frozen onto the
  base prompt.

## The `oc awareness` commands

| Command | Purpose |
|---|---|
| `review` | What the agent believes about you — top-K with provenance. |
| `explain <id>` / `explain --session` | One fact's provenance, or the reranker score breakdown. |
| `forget <id>` | Forget a wrong fact (reversible soft-delete). |
| `correct <id> <new>` | Replace a wrong value with the right one. |
| `migrate` | Clean legacy cruft — flag noise, collapse duplicate edges. |
| `eval-ranker` | Compare the reranker against the old static sort. |
| `debug` | JSON state dump for a bug report. |

Full reference: **[cli.md](cli.md)**. (`oc user-model` is the lower-level
developer CRUD surface.)

## Sub-documents

- **[cli.md](cli.md)** — every `oc awareness` subcommand, with examples.
- **[reranker.md](reranker.md)** — the scoring model: terms, weights, BM25,
  decay, drift.
- **[life-events.md](life-events.md)** — life-event "teeth": hint injection and
  tone directives for detected life events, self-correcting check-in crons, CLI
  controls, and v1 limitations.
- **[../refs/oc-user-model-baseline.md](../refs/oc-user-model-baseline.md)** —
  what was wired vs. dormant before the cleanup.
- **[../refs/oc-user-model-writers.md](../refs/oc-user-model-writers.md)** —
  the writer audit + the edge-explosion finding.

## What the cleanup shipped

| Milestone | Outcome |
|---|---|
| M1 | `oc awareness review/explain/forget/correct` — inspect & fix the graph. |
| M2 | `NodeKindValidator` + edge-idempotent importer + `oc awareness migrate`. |
| M3 | Context-aware reranker replaces the static `(kind, confidence)` sort. |
| M4 | Decay runs (daily cron tick); reranker consumes edge-recency + drift. |
| M5 | `oc awareness debug` + this documentation. |

## Operational note

`oc awareness migrate` ships **dry-run by default**. To clean a graph that
accumulated noise before this work, run `oc awareness migrate --apply` — it
flags agent-internal-noise facts `needs_review` (excluding them from the
prompt) and collapses duplicate edges. Until then the validator only stops
*new* noise; existing noise stays until the migration is run.
