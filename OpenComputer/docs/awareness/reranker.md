# The context-aware reranker

The prompt's `<user-facts>` block used to be ranked by `(kind, confidence)`
alone — static, and blind to what the conversation was about. Milestone 3
replaced that with `UserFactsReranker` (`opencomputer/user_model/reranker.py`).

## The score

Each candidate fact gets a composite score, a weighted blend of four terms,
each normalised to `[0, 1]`:

```
score = w_kind·kind  +  w_conf·confidence  +  w_recency·recency  +  w_bm25·bm25
```

| Term | Meaning |
|---|---|
| `kind` | Kind priority — identity 1.0, goal 0.8, preference 0.6, attribute 0.4, relationship 0.2. |
| `confidence` | The node's stored confidence. |
| `recency` | `0.5 ** (age_days / 30)` — a fact last asserted 30 days ago contributes half. |
| `bm25` | Okapi BM25 (k1=1.5, b=0.75) of the fact value against the session's opening message, max-normalised across the candidate set. |

Default weights (`RerankWeights`): kind 0.40, confidence 0.20, recency 0.20,
bm25 0.20 — they sum to 1.0 so the composite stays in `[0, 1]`.

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

## Deferred

- **Config-yaml weight overrides.** The reranker ships with the
  `RerankWeights` defaults; a `<profile>/config.yaml` override path is
  deferred. `eval-ranker` and `explain --session` make the current ranking
  inspectable in the meantime.
- **Embedding-based relevance.** BM25 is the v1 relevance signal.
