# The context-aware reranker

The prompt's `<user-facts>` block used to be ranked by `(kind, confidence)`
alone — static, and blind to what the conversation was about. Milestone 3
replaced that with `UserFactsReranker` (`opencomputer/user_model/reranker.py`).

## The score

Each candidate fact gets a composite score, a weighted blend of five terms,
each normalised to `[0, 1]`:

```
score = w_kind·kind + w_conf·confidence + w_recency·recency
        + w_bm25·bm25 + w_drift·drift
```

| Term | Meaning |
|---|---|
| `kind` | Kind priority — identity 1.0, goal 0.8, preference 0.6, attribute 0.4, relationship 0.2. |
| `confidence` | The node's stored confidence. |
| `recency` | Blend of `last_seen_at` age decay and the decay engine's edge-`recency_weight` aggregate (see *Decay and drift* below). |
| `bm25` | Okapi BM25 (k1=1.5, b=0.75) of the fact value against the session's opening message, max-normalised across the candidate set. |
| `drift` | `1 − drift_score` — a fact disputed by `contradicts` edges loses standing. Default weight 0 (see *Decay and drift*). |

Default weights (`RerankWeights`): kind 0.40, confidence 0.20, recency 0.20,
bm25 0.20, drift 0.0. The reranker **renormalises** the active weight set,
so the composite is always in `[0, 1]` regardless of the configured weights
or whether BM25 is active.

## BM25 — the session-relevance term

Pure-Python, no external dependency. The "query" is the session's opening
user message; each candidate node value is a "document". Tokenisation is
lowercase + split on non-alphanumerics + a small stopword drop — deliberately
lossy (URLs and snake_case fragment into their alphanumeric runs). Good
enough for short fact values vs. a short opening message.

## Context-free mode

Cron and gateway runs have no conversation. With no session messages the
reranker enters **context-free mode**: the BM25 term is dropped and the
remaining three weights are renormalised, so a non-interactive run still
gets a sensible static ranking (kind + confidence + recency).

## Ranked once per session — by design

`build_user_facts` is computed **once per session** and frozen onto the base
system prompt — that is what preserves Anthropic prefix-cache hits on turn 2+.
The reranker therefore runs once, against the session's *opening* message,
and the result is frozen for the session. It deliberately does **not**
re-rank mid-session: doing so would mutate the base prompt and miss the
prefix cache on every re-rank. (This is a deliberate departure from the
original plan's "invalidate every K turns" — that idea predated noticing the
frozen-base design.)

## What the reranker excludes

`build_user_facts` drops, before ranking:

- **soft-deleted** nodes (`oc awareness forget` / `correct`),
- **`needs_review`** nodes (`oc awareness migrate`).

So a `forget` / `correct` / `migrate` action now takes effect in the actual
injected prompt, not only in `oc awareness review`.

## Observability

- `oc awareness eval-ranker [--query TEXT]` — the old `(kind, confidence)`
  sort beside the reranker's ranking, with a count of how many positions
  moved. `--query` simulates a session opening message.
- `oc awareness explain --session [--query TEXT]` — the per-term score
  breakdown (kind / confidence / recency / BM25 sub-scores + composite) for
  the top facts.

## Decay and drift

**Recency.** The recency term blends two signals: `0.5 ** (age_days / 30)`
from the node's `last_seen_at`, and — when the node has edges — the mean
`recency_weight` of those edges (`UserModelStore.node_recency_score`). The
decay engine ages edge `recency_weight` exponentially by edge-kind half-life;
a daily-gated cron tick (`_run_decay_tick` in `cron/system_jobs.py`) runs the
pass, because the bus-attached `DecayDriftScheduler` is never instantiated in
the running agent. Edgeless nodes (most profile-bootstrap facts) use the
`last_seen_at` signal alone.

**Drift.** `UserModelStore.node_drift_score` returns `1 − Π(1 −
source_reliability)` over the `contradicts` edges pointing at a node — a
`[0, 1]` penalty. The reranker's drift term is `1 − drift_score`. The default
`w_drift` is **0.0**: the term is fully plumbed and tested, but inert until a
contradiction detector starts writing `contradicts` edges (none does today).
Raise `w_drift` to enable it once that signal exists.

## Deferred

- **Config-yaml weight overrides.** The reranker ships with the
  `RerankWeights` defaults; a `<profile>/config.yaml` override path is
  deferred. `eval-ranker` and `explain --session` make the current ranking
  inspectable in the meantime.
- **A contradiction detector** to feed the drift term — `w_drift` stays 0
  until one exists.
- **Embedding-based relevance.** BM25 is the v1 relevance signal.
