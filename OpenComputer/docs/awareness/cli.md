# `oc awareness` — inspect & correct what the agent knows about you

The agent maintains a **user-model graph** (`<profile>/user_model/graph.sqlite`) —
identity facts, goals, preferences, and attributes it has learned from your
behaviour, your profile bootstrap, and explicit statements. The top-ranked facts
are injected into every prompt as the `<user-facts>` block.

`oc awareness` is the surface for **seeing and fixing** that graph. When the
agent learns something wrong, this is your recourse.

> Sibling command: `oc user-model` is the lower-level, developer-facing graph
> CRUD (`nodes list`, `edges list`, `import-motifs`, `decay run`, `drift …`).
> `oc awareness` is the user-facing trust surface — provenance and corrections.

---

## `oc awareness review`

Show what the agent currently believes about you — the top-K facts in the same
priority order the prompt uses, with provenance and a contradiction flag.

```bash
oc awareness review                 # top 50 facts
oc awareness review --all           # every fact
oc awareness review --limit 20      # top 20
oc awareness review --deleted       # include soft-deleted (forgotten) facts
```

Columns: short **id** (use it with the other commands), **kind**
(identity / goal / preference / attribute / relationship), **conf**idence,
**last seen**, **source** (dominant provenance of the fact's edges — e.g.
`motif_importer`, `honcho_synthesis`, `user_explicit`, or `—` for an
edge-less node), **flags** (`⚠×N` = N facts contradict this one), and **value**.

## `oc awareness explain <id>`

Full provenance for one fact: its fields, every incident edge, and — per edge —
both the stored `recency_weight` and the weight temporal decay would assign
right now. A large gap means the decay scheduler has not run.

```bash
oc awareness explain 3f9a1c20       # full id OR any unique prefix
```

`<id>` accepts a full node id or any unique prefix (git-style) — the 8-char form
`review` prints is enough. An ambiguous prefix lists the candidates.

## `oc awareness forget <id>`

Forget a fact the agent learned wrong.

```bash
oc awareness forget 3f9a1c20            # soft-delete (reversible)
oc awareness forget 3f9a1c20 --hard     # drop the row and its edges
oc awareness forget 7c2e... --confirm   # required for identity facts
```

- **Default — soft-delete.** The row is kept but flagged `deleted`: hidden from
  prompts and `review`, still visible under `review --deleted` and `explain`.
  Its edges are preserved, so the deletion is auditable and reversible.
- **`--hard`.** Drops the node row outright; its incident edges cascade away.
- **`--confirm`.** Identity facts (your name, email, city, …) are foundational.
  Forgetting one requires `--confirm`; without it the command refuses and
  reports how many edges depend on the fact.

## `oc awareness correct <id> <new-value>`

Correct a fact — replace a wrong value with the right one.

```bash
oc awareness correct 3f9a1c20 "lives in San Francisco"
oc awareness correct 7c2e... "name: Saksham" --confirm    # identity → --confirm
```

`correct` does three things atomically:

1. Creates a node carrying the corrected value (same kind, confidence 1.0 — an
   explicit correction is the most trustworthy signal).
2. Records a `supersedes` edge **new → old** — the durable provenance the
   context reranker (Milestones 3–4) reads to prefer the correction.
3. Soft-deletes the old node, so the fix takes effect immediately rather than
   only once the reranker ships.

Correcting a fact to its current value is a no-op. Identity facts require
`--confirm`.

---

## The soft-delete model

`forget` (default) and `correct` both **soft-delete** by setting
`metadata.deleted = True` on the node via an in-place update — *not* by
re-inserting the row. Re-inserting (`INSERT OR REPLACE`) would cascade-drop the
node's edges through the `ON DELETE CASCADE` foreign keys; the in-place update
preserves them, keeping the soft-delete reversible and the provenance intact.

Soft-deleted facts are excluded from `review` and the prompt's `<user-facts>`
block, but remain in the graph for `review --deleted` and `explain`.
