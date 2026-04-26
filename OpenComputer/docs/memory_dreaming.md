# Episodic-Memory Dreaming (EXPERIMENTAL)

> **Status:** EXPERIMENTAL — gate before promotion to default.
>
> Round 2A P-18 ships the plumbing only. Do **not** enable this on a
> production profile until you have read this document end-to-end and
> verified the trade-offs against your own usage pattern.

## What it is

A "dream" is a background turn that consolidates recent **episodic
memory** entries — the one-row-per-completed-conversation-turn summaries
that already populate `episodic_events` in `~/.opencomputer/sessions.db`
— into per-cluster theme summaries.

The goal is corpus hygiene: as a profile accumulates thousands of
turn-level events, the FTS5 index gets noisier and `opencomputer recall
QUERY` returns more low-signal hits. Dreaming folds entries that share a
working-context (same week, overlapping topic keywords) into a single
short bullet list, marks the originals as `dreamed_into = <consolidation
row id>`, and keeps the consolidation searchable in the same FTS5
index.

## Why it is OFF by default

The plan locks `MemoryConfig.dreaming_enabled = False` (decision L6 in
`~/.claude/plans/2026-04-26-round-2a-execution.md`). Reasons:

1. **Quality risk.** The cheap auxiliary model used for consolidation
   may smooth over nuance the user cared about. Until we have a side-by-
   side dogfood comparison ("recall before vs. after dreaming"), the
   default must preserve the raw turn-level history.
2. **Cost.** Even on the cheap route, every cluster is one provider
   call. Hourly schedules on a busy profile can rack up a few cents a
   day — small but visible.
3. **Idempotency contract.** The runner is idempotent (re-running
   `dream-now` only consolidates rows where `dreamed_into IS NULL`) but
   we have not yet shipped a way to *un-dream* a consolidation if the
   user finds it unhelpful. Until that exists, opt-in is the safe
   posture.

## Enabling it

```bash
# Manual one-shot — runs even when dreaming is disabled in config.
opencomputer memory dream-now

# Persist the toggle. Today's CLI does NOT start a background scheduler;
# users wire cron / launchd / systemd to call dream-now on the chosen
# cadence. The flag is consulted by `memory doctor` and by future
# scheduler work.
opencomputer memory dream-on --interval daily
opencomputer memory dream-on --interval hourly

# Disable. Existing consolidation rows are NOT removed.
opencomputer memory dream-off
```

Optional flags on `dream-now`:

- `--session-id <id>` restricts consolidation to one session's
  episodic events (handy when dogfooding on a single chat).
- `--limit N` caps how many undreamed rows the pass reads (default 50).

## How clustering works

KISS by design — no embeddings, no second LLM call.

1. Fetch up to `--limit` rows from `episodic_events` where
   `dreamed_into IS NULL`, oldest first.
2. For each row, compute:
   - **date bucket** = ISO week (`YYYY-Www`, UTC).
   - **topic keywords** = lowercase tool names ∪ file basenames ∪
     ≥3-char tokens from the summary (minus a small stopword list).
3. Walk the rows in order. For each row, find the first existing
   cluster in the same bucket whose keyword set intersects this row's;
   if none, start a new cluster.
4. Skip clusters with fewer than `MIN_CLUSTER_SIZE` (= 2) entries.
5. For each kept cluster, call the configured **cheap** model (or the
   main model if no cheap is configured) with a short prompt asking for
   ≤ 5 bullets summarizing themes + facts.
6. Persist the consolidation row (`turn_index = -1` flags it as
   agent-generated) and stamp originals with `dreamed_into = <id>` in
   the same SQLite transaction.

## Failure handling

- Provider error on a cluster → retry once. If that also fails, log a
  warning and skip the cluster. Originals stay untouched (so the next
  `dream-now` retries them).
- Empty store → no-op (returns a zero-counted `DreamReport`).
- DB lock contention is handled by `SessionDB._txn`'s retry+jitter
  loop; nothing dreaming-specific.

## Promotion gate

Before flipping `MemoryConfig.dreaming_enabled = True` in
`default_config()`:

- [ ] Side-by-side: run `opencomputer recall <real-query>` against a
  profile with dreaming OFF for ≥ 7 days, then again with dreaming ON
  for ≥ 7 days. Document precision/recall delta in `docs/dreaming-
  dogfood-N.md`.
- [ ] Cost report: dump `pick-stats`-style numbers — turns dreamed,
  $$ spent, average bullets-per-cluster. Confirm < $1/day on a typical
  profile.
- [ ] Un-dream path: ship `opencomputer memory undream
  <consolidation_id>` (clears `dreamed_into` on originals + deletes the
  consolidation) so users can recover from a bad pass.
- [ ] Scheduler: replace the cron/launchd handoff with a built-in
  cadence runner gated on `dreaming_interval`.
- [ ] Documentation: replace this EXPERIMENTAL banner with a stable
  reference and add a CHANGELOG entry.

Until **all four** are checked, `dreaming_enabled` stays `False` in
`default_config()`. The CLI is shipped specifically so dogfood can
begin; that is the only intended use today.

## Related code

- `opencomputer/agent/dreaming.py` — runner + clustering + prompt builder.
- `opencomputer/agent/state.py` — `dreamed_into` column (schema v4)
  + `SessionDB.list_undreamed_episodic` /
  `SessionDB.record_dream_consolidation`.
- `opencomputer/cli_memory.py` — `dream-now` / `dream-on` /
  `dream-off` subcommands; `memory doctor` row.
- `tests/test_memory_dreaming.py` — clustering, idempotency, retry,
  toggle, no-op-on-empty.
