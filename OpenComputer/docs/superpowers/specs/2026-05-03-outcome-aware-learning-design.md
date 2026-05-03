# Outcome-Aware Learning System — Design Spec

**Status:** Draft (2026-05-03)
**Author:** Saksham (with /brainstorming refinement loop)
**Goal:** Let the agent learn from the consequences of its actions without depending on explicit user feedback. Three phases: passive recording, outcome scoring, reversible adaptive loop.

---

## Hard Constraints

1. **No explicit-feedback dependency.** No thumbs-up/down, no rating UI, no mandatory comment box. All signals must be inferred from observable behavior.
2. **No fine-tuning.** LLM weights are frozen via API. The action space is bandit-style policy optimization on discrete knobs.
3. **No one-way doors.** Every policy change must be reversible. Skills get deprioritized but stay loadable. Tools get gated more strictly but stay registered. Prompt variants get demoted but stay archived. Recall ranking gets penalized but never deleted.
4. **Honcho session-serialization preserved.** New writes per `session_id` must be ordered (per-session async lock).
5. **Auditability.** Asking "what changed in the last 7 days and why" must produce a clean answer.

---

## Architecture: Three Phases

| Phase | Goal | User-visible? | Behavior change? |
|---|---|---|---|
| **0. Passive Recording** | Capture per-turn implicit signals into structured rows | No | None |
| **1. Outcome Scoring** | Fuse signals into composite + LLM-judge `turn_score` | Dashboard only | None |
| **2. v0 Adaptive Loop** | One reversible knob: per-memory `recall_penalty`, with full audit + statistical revert + progressive trust ramp | Yes (`/policy-changes`, `/policy-revert`, Telegram approval) | Yes (memory ranking) |

Phase 0 ships independently — it has standalone value as a dashboard substrate. Phase 1 layers scoring on top. Phase 2 v0 closes the loop with one knob.

---

# Phase 0 — Passive Recording

**Effort:** ~2 days. **Risk:** Low. **Ships first.**

## Goal

Record per-turn implicit signals into a single queryable row per `(session_id, turn_index)`. Zero user-facing change. Zero LLM cost. The data accumulates as substrate for everything later.

## New table: `turn_outcomes` (migration v7)

```sql
CREATE TABLE turn_outcomes (
    id                          TEXT PRIMARY KEY,
    session_id                  TEXT NOT NULL,
    turn_index                  INTEGER NOT NULL,
    created_at                  REAL NOT NULL,

    -- Tool call signals (sourced from tool_usage table)
    tool_call_count             INTEGER DEFAULT 0,
    tool_success_count          INTEGER DEFAULT 0,
    tool_error_count            INTEGER DEFAULT 0,
    tool_blocked_count          INTEGER DEFAULT 0,
    self_cancel_count           INTEGER DEFAULT 0,    -- write_file→delete_file same path within 60s
    retry_count                 INTEGER DEFAULT 0,    -- consecutive same-tool calls after error

    -- User signals
    vibe_before                 TEXT,                 -- snapshot of vibe at turn start
    vibe_after                  TEXT,                 -- vibe at next user message
    reply_latency_s             REAL,                 -- seconds; NULL if no reply
    affirmation_present         INTEGER DEFAULT 0,    -- regex hit on next user message
    correction_present          INTEGER DEFAULT 0,    -- regex hit on next user message
    conversation_abandoned      INTEGER DEFAULT 0,    -- no follow-up within 24h

    -- System signals
    standing_order_violations   TEXT,                 -- JSON array
    duration_s                  REAL,                 -- total turn duration

    schema_version              INTEGER DEFAULT 1,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX idx_turn_outcomes_session ON turn_outcomes(session_id, turn_index);
CREATE INDEX idx_turn_outcomes_created ON turn_outcomes(created_at);
```

## Hooks

| Hook point | File | What it does |
|---|---|---|
| Post-`run_conversation()` write | `gateway/dispatch.py` (after line 500 in `_do_dispatch`) | Async write of `turn_outcomes` row. **Outside the loop critical path.** |
| Self-cancel sweep (cron) | `cron/jobs/turn_outcomes_sweep.py` (new) | Every 5 min: scan recent `tool_usage` for write→delete, create→cancel patterns within same session; backfill `self_cancel_count` |
| Abandonment sweep (cron) | Same file | Every 1 hr: mark sessions with no activity > 24h as abandoned; backfill `conversation_abandoned` for last assistant turn |
| Affirmation/correction lexicon | `agent/affirmation_lexicon.py` (new, ~30 LOC) | Regex patterns: `\b(thanks|thank you|perfect|exactly|that worked|yes that's right)\b` for affirmation; `\b(no|wrong|that's not|actually|incorrect|undo)\b` for correction |
| Per-session Honcho lock | `extensions/memory-honcho/provider.py` | `_session_locks: dict[str, asyncio.Lock]` with `defaultdict`-style creation; all `sync_turn` and new `sync_outcome` calls take the lock |

## Honcho expansion

Today Honcho receives only plaintext user↔assistant text. Phase 0 adds structured observations:

- `ToolCallObservation(session_id, turn_index, tool, args_summary, outcome, duration_s)`
- `ToolErrorObservation(session_id, turn_index, tool, error_class, error_msg)`
- `SelfCancelObservation(session_id, turn_index, original_tool, undo_tool, gap_s)`
- `VibeDriftObservation(session_id, turn_index, vibe_before, vibe_after)`

Sent **in addition to** existing `sync_turn()` plaintext, **gated by per-session `asyncio.Lock`** to preserve serialization (this is the only structural footgun identified during code mapping).

## Day-30 deliverables

- Dashboard query: "what fraction of last 1000 turns ended with tool error?"
- Cohort query: "show me turns where `standing_order_violations` is non-empty"
- Memory bootstrap: 30 days of trajectories ready for Phase 1 scoring
- Honcho dialectic now sees tool/error/self-cancel context, not just dialog

---

# Phase 1 — Outcome Scoring

**Effort:** ~1 week. **Risk:** Medium (judge bias). **Ships after Phase 0 has 14 days of data.**

## Goal

Produce a composite `turn_score ∈ [0, 1]` per turn from two sub-scores (composite signal-fusion + LLM-judge), then a fused final score.

## Migration v8 — extend `turn_outcomes`

```sql
ALTER TABLE turn_outcomes ADD COLUMN composite_score REAL;     -- weighted sum, no LLM
ALTER TABLE turn_outcomes ADD COLUMN judge_score REAL;          -- 0.0–1.0 from Haiku
ALTER TABLE turn_outcomes ADD COLUMN judge_reasoning TEXT;
ALTER TABLE turn_outcomes ADD COLUMN judge_model TEXT;          -- e.g. claude-haiku-4-5
ALTER TABLE turn_outcomes ADD COLUMN turn_score REAL;           -- final fused
ALTER TABLE turn_outcomes ADD COLUMN scored_at REAL;
```

## Composite scorer (no LLM, in-process)

```
composite_score = clip(0, 1,
    0.50                                              # baseline so silence doesn't crash to zero
  + 0.20 * tool_success_rate                          # success / (success + error + 1)
  - 0.15 * normalize(self_cancel_count, max=2)
  - 0.15 * normalize(retry_count, max=3)
  - 0.10 * (1 if conversation_abandoned else 0)
  + 0.10 * (1 if affirmation_present else 0)
  - 0.15 * (1 if correction_present else 0)
  + 0.05 * vibe_delta_signed                          # +1 improved, -1 degraded, 0 same
  - 0.10 * normalize(standing_order_violation_count, max=3)
)
```

Weights chosen to prevent reward hacking (see Reward-Hacking Traps section below).

## LLM judge

- **Hook:** Reuses `agent/reviewer.py:spawn_review()` (already async, fire-and-forget; designer left a hook for v2 LLM upgrade).
- **Model:** `claude-haiku-4-5` for cost (~$0.001/turn).
- **Cost guard:** Subject to `cost_guard.check_budget()` — if budget exhausted, judge skipped, `judge_score = NULL`.
- **Prompt:** Trajectory + standing orders + composite breakdown → returns `<judge_score>0.72</judge_score><reasoning>…</reasoning>`.
- **Disagreement audit:** If `|composite - judge| > 0.4`, log `judge_disagreement` event for human review. May indicate signal mis-calibration.

## Fused score

```
turn_score = 0.4 * composite_score + 0.6 * judge_score   when both available
turn_score = composite_score                             when judge skipped
```

The judge dominates because it can read trajectory semantics (e.g., user's "no, that's wrong" is encoded in correction_present but the *severity* of the correction needs LLM context).

---

# Phase 2 v0 — Adaptive Loop (single knob, full reversibility)

**Effort:** 1.5–2 weeks. **Risk:** Medium-high (first closed-loop adaptation). **Ships after Phase 1 has 14 days of scored data.**

## The Knob: per-memory `recall_penalty`

A soft decaying float on `episodic_events`, applied multiplicatively to BM25 score in `recall_synthesizer.py`.

### Why this knob (not skill re-ranking, not prompts, not tool gating)

1. **Reversibility is the data structure.** Penalty decays exponentially toward 0 over ~60 days. Soft rollback is automatic. Explicit revert is `UPDATE … SET recall_penalty = 0`.
2. **Blast radius is per-record.** Bad penalty on one memory affects only that memory's ranking.
3. **Recall already has BM25.** Adding `(1 − decayed_penalty)` multiplier is ~30 LOC. Skills today have NO weight field — re-ranking would mean inventing a ranking system AND a reversibility layer simultaneously (two greenfield projects).
4. **Pattern alignment.** The user's exact framing in turn 1 was "recall ranking gets penalized but never deleted, soft decaying float." This spec is implementing that.
5. **Substrate, not destination.** v0's purpose is to teach the codebase the audit-recommendation-monitor-revert pattern on the lowest-risk knob available. Higher-impact knobs come in v0.5+.

### Effect

```python
# In recall_synthesizer.py BM25 result loop:
adjusted_score = base_bm25_score * max(0.05, 1 - recall_penalty * decay_factor(age_days))
```

- Floor of 0.05: penalized memories are suppressed but never literally unreachable
- `decay_factor(d) = 0.95 ** d`: penalty halves about every 14 days; reaches ~0 in 60 days

## Migration v9

```sql
ALTER TABLE episodic_events ADD COLUMN recall_penalty REAL DEFAULT 0.0;
ALTER TABLE episodic_events ADD COLUMN recall_penalty_updated_at REAL;
```

## Recommendation engine — Pushback #3 (named, dumb, replaceable)

**Engine: `MostCitedBelowMedian/1`** — explicitly v0-on-purpose.

```
ELIGIBILITY:
  Memory was cited (returned by recall_synthesizer) at least 5 times in last 14 days.
  AND has not been adjusted in last 7 days (avoid double-penalizing).

SELECTION:
  Candidates = all eligible memories.
  Score each by mean downstream turn_score across its citation turns.
  Pick the candidate with the lowest mean.

NO-OP CHECK:                                   # Addition #4
  IF candidate's mean turn_score >= (corpus_median - minimum_deviation_threshold):
    Recommend zero changes. Log "No-op night."
    Return.

TIE-BREAKERS (in order):
  (1) Higher citation count (more confidence in signal)
  (2) Older recall_penalty_updated_at

RECOMMENDATION:
  Increase recall_penalty by +0.20.
  Cap total penalty at 0.80 (preserves recovery space; 0.05 floor + 0.20 unit + decay).

VERSION:
  Engine emits 'MostCitedBelowMedian/1' into policy_changes.recommendation_engine_version.
```

**Documented as v0-on-purpose:** This heuristic does not learn weights. It does not consider memory quality. It does not detect distribution shift. It is a placeholder so we ship the *loop*, not the *brain*. v0.5 will replace it.

## Progressive trust ramp — Pushback #1 (first-class)

```
Setting: auto_approve_after_n_safe_decisions  (default: 10)
Persisted in: ~/.opencomputer/feature_flags.json

PHASE A — Explicit approval (until N safe decisions accumulated):
  Recommendation status: 'pending_approval'
  Telegram notification: "Engine recommends X. /policy-approve <id> or ignore (auto-discard in 7 days)."
  No automatic application.

PHASE B — TTL auto-approve (after N safe decisions):
  Recommendation auto-applied with revert_after = now() + 7 days
  Telegram notification: "Auto-approved X. Reason: Y. /policy-revert <id> if undesired."
  Statistical auto-revert applies (see below).

DEFINITION of "safe decision":
  policy_change reached status = 'expired_decayed' (not reverted, decayed naturally)
  OR status = 'active' for >= 30 days without any user revert.

This protects against premature trust:
  - Each transition through the trust ramp is logged
  - User can always enforce explicit-approval mode by setting N very high
  - Reverting a change resets the safe-decision counter (a revert means the engine got it wrong)
```

## Statistical auto-revert — Pushback #2 (first-class)

Replaces the originally-handwaved "X% degraded" with a proper test.

```
Cron job: auto_revert_due() — every 6 hours

For each policy_change WHERE status = 'pending_evaluation':

  eligible_turn_count = COUNT of turns since ts_applied where this memory was in
                        recall_synthesizer's eligibility set (i.e., would have been
                        cited under penalty=0; we still simulate this even when
                        penalty suppresses it, by pre-computing "would-have-been-cited"
                        from BM25 base score)

  IF eligible_turn_count < min_eligible_turns_for_revert (default 10):
    status stays 'pending_evaluation'.    # Hard gate: never auto-revert on small samples.
    continue

  post_change_mean = mean(turn_score) on those eligible turns
  baseline_mean = pre_change_baseline_mean   # captured at apply time
  baseline_std = pre_change_baseline_std

  IF post_change_mean < (baseline_mean - revert_threshold_sigma * baseline_std):
    auto_revert(reason=f"statistical: post-mean {post:.3f} < baseline {base:.3f} - {sigma}σ")
    status = 'reverted'

  ELSE IF post_change_mean within ±1σ of baseline:
    status = 'active'                     # passed evaluation; soft-decay continues
    increment safe_decision_counter (for trust ramp)

  ELSE IF post_change_mean > baseline + 1σ:
    status = 'active' (positive)          # change improved things; keep
    increment safe_decision_counter
```

**Sample-size hygiene is a hard gate.** Below N=10, we explicitly do not revert. The cron job is idempotent and re-evaluates each run.

## No-op path — Addition #4 (first-class)

The recommendation engine MUST be allowed to emit zero changes per night. Forcing changes on quiet days is a known reward-hacking pattern in adaptive systems.

```
Setting: minimum_deviation_threshold  (default 0.10)

If no candidate's mean turn_score deviates more than threshold below corpus median:
  - Engine emits zero recommendations
  - Cron job logs: "No-op night: top candidate {id} mean={x:.3f} vs median {m:.3f}, gap {g:.3f} below threshold {t:.3f}"
  - No policy_changes row created

Quiet days produce no rows. The audit trail correctly shows "nothing happened on day X" rather than "we forced a change."
```

## Kill switch — Addition #5 (first-class)

```
File: ~/.opencomputer/feature_flags.json

{
  "policy_engine": {
    "enabled": true,
    "auto_approve_after_n_safe_decisions": 10,
    "daily_change_budget": 3,
    "min_eligible_turns_for_revert": 10,
    "revert_threshold_sigma": 1.0,
    "decay_factor_per_day": 0.95,
    "minimum_deviation_threshold": 0.10
  }
}

WHEN policy_engine.enabled = false:
  - Recommendation engine cron job is a no-op
  - In-flight active changes continue to soft-decay (no auto-wipe)
  - /policy-revert <id> still works manually
  - /policy-changes still queryable
  - Trust counter is preserved

NEW MODULE: opencomputer/agent/feature_flags.py
  - read_flag(path: str, default) -> Any
  - write_flag(path: str, value: Any) -> None  (atomic file lock + audit-log append)
  - All writes mirrored to consent/audit.py HMAC chain for tamper-evidence

CLI:
  oc policy enable
  oc policy disable
  oc policy status                    # shows current flag values
```

This is the fail-safe: one config edit halts automated decisions without removing audit/manual control.

## Daily budget — Addition #6 (first-class)

```
Setting: daily_change_budget  (default 3, in feature_flags.json)

BEFORE any recommendation engine run:
  applied_today = SELECT COUNT(*) FROM policy_changes
                  WHERE ts_applied > now() - 86400
                  AND status NOT IN ('reverted')

  IF applied_today >= daily_change_budget:
    Skip this run. Log: "Daily budget hit ({applied}/{budget}). Sleeping until tomorrow."
    Telegram notification (optional): "Engine skipped — daily budget hit."

The cap is computed at recommendation time, not application time, so a flurry of
pending_approval drafts cannot sneak past during Phase A.

In Phase A (explicit approval), the budget counts approved-and-applied changes
(not pending drafts). In Phase B (auto-approve), it counts auto-applied changes.
```

A circuit breaker. Even if the engine sees 50 plausible candidates, only 3 land per 24h.

## `policy_changes` table (migration v9)

```sql
CREATE TABLE policy_changes (
    id                              TEXT PRIMARY KEY,
    ts_drafted                      REAL NOT NULL,
    ts_applied                      REAL,
    knob_kind                       TEXT NOT NULL,        -- 'recall_penalty' (only kind in v0)
    target_id                       TEXT NOT NULL,         -- episodic_events.id
    prev_value                      TEXT NOT NULL,         -- JSON
    new_value                       TEXT NOT NULL,         -- JSON
    reason                          TEXT NOT NULL,
    expected_effect                 TEXT,
    revert_after                    REAL,                  -- nullable; when set, eligible for auto-revert
    rollback_hook                   TEXT NOT NULL,         -- JSON: {action, field, value}
    recommendation_engine_version   TEXT NOT NULL,         -- e.g. 'MostCitedBelowMedian/1'

    -- Approval (progressive trust ramp)
    approval_mode                   TEXT NOT NULL,         -- 'explicit' | 'auto_ttl'
    approved_by                     TEXT,                  -- user id or 'auto'
    approved_at                     REAL,

    -- HMAC chain (reuses consent/audit.py pattern)
    hmac_prev                       TEXT NOT NULL,
    hmac_self                       TEXT NOT NULL,

    -- Status & evaluation
    status                          TEXT NOT NULL,
        -- 'drafted' | 'pending_approval' | 'pending_evaluation'
        -- | 'active' | 'reverted' | 'expired_decayed'
    eligible_turn_count             INTEGER DEFAULT 0,
    pre_change_baseline_mean        REAL,
    pre_change_baseline_std         REAL,
    post_change_mean                REAL,
    reverted_at                     REAL,
    reverted_reason                 TEXT
);

CREATE INDEX idx_policy_changes_status ON policy_changes(status);
CREATE INDEX idx_policy_changes_target ON policy_changes(knob_kind, target_id);
CREATE INDEX idx_policy_changes_engine ON policy_changes(recommendation_engine_version);
```

## New CLI / slash commands

| Command | Purpose | Reuses |
|---|---|---|
| `/policy-changes [--days N]` | Audit query: last N days of changes (default 7) with engine, reason, status, revert link | `cli_ui/slash_handlers.py` framework |
| `/policy-approve <id>` | Approve a `pending_approval` change → applies it | New handler + `policy_changes` UPDATE |
| `/policy-revert <id>` | Manual revert at any state; writes new HMAC link | `consent/audit.py` chain pattern |
| `oc policy show` | CLI alias for `/policy-changes` | `cli.py` |
| `oc policy enable` / `oc policy disable` / `oc policy status` | Flip kill switch | `feature_flags.py` |

## Reuse Map (full)

| New piece | Reuses |
|---|---|
| `policy_changes` HMAC chain | `agent/consent/audit.py` (HMAC-SHA256, `verify_chain()`) |
| `PolicyChangeEvent`, `PolicyRevertedEvent` on bus | `ingestion/bus.py` TypedEventBus |
| Pre-change baseline capture | `snapshot/quick.py` (forensic record only — NOT the rollback substrate) |
| Drafting + approve workflow | `evolution/store.py` quarantine→approved pattern |
| Auto-revert + sweep cron | `cron/scheduler.py` at-most-once execution |
| Recommendation engine module | New `opencomputer/evolution/policy_engine.py` (~250 LOC) |
| Recall penalty application | `agent/recall_synthesizer.py` (~30 LOC patch) |
| Slash command framework | `cli_ui/slash_handlers.py` |
| Telegram notification | Existing channel adapter |
| `feature_flags.json` write | New `agent/feature_flags.py` (~100 LOC) |

**Greenfield code total: ~600 LOC across all 3 phases.**

---

# Acceptance Criteria

## Phase 0 — Passive Recording
1. Every completed turn produces exactly one `turn_outcomes` row.
2. Self-cancel sweep correctly identifies write_file→delete_file with same path within 60s in same session.
3. Abandonment sweep marks last assistant turn of inactive sessions correctly.
4. Honcho writes are serialized per `session_id` (verified by stress test: 100 concurrent turns, no race-condition lost messages).
5. Phase 0 adds **<50ms to per-turn p99 wall-clock latency** (verified by latency benchmark).

## Phase 1 — Outcome Scoring
6. `composite_score` is computable from `turn_outcomes` alone (no LLM call).
7. LLM judge runs async; if cost-guard budget exceeded, `judge_score = NULL` but `turn_score` still emitted from composite alone.
8. `|composite - judge| > 0.4` produces a `judge_disagreement` log event.
9. **95% of turns are scored within 60s of completion.**

## Phase 2 v0 — Full Reversibility Loop

**Statistical auto-revert (Pushback #2 acceptance):**

10. With `eligible_turn_count >= 10` AND `post_change_mean < pre_change_mean - 1σ`: auto-revert fires.
11. With `eligible_turn_count < 10`: status stays `pending_evaluation`; **never auto-reverts on small samples.**
12. With `eligible_turn_count >= 10` AND post-mean within ±1σ of baseline: status → `active`, safe-decision counter increments.

**Progressive trust ramp (Pushback #1 acceptance):**

13. First 10 recommendations require explicit `/policy-approve <id>`.
14. Telegram notification fires on every `pending_approval` recommendation.
15. After 10 successful unrevert decisions (status reaches `expired_decayed` OR `active` for ≥30 days), recommendations auto-apply with TTL.
16. Reverting an auto-applied change does NOT decrement the safe-decision counter retroactively (history is immutable), but it RESETS the next-trust-window counter.

**Named heuristic + version (Pushback #3 acceptance):**

17. Every `policy_changes` row carries `recommendation_engine_version` (e.g. `MostCitedBelowMedian/1`).
18. v0 engine emits zero changes on quiet days (no candidate exceeds `minimum_deviation_threshold`).

**No-op path (Addition #4):**

19. Days where no candidate exceeds threshold produce zero `policy_changes` rows AND a single log line.

**Kill switch (Addition #5):**

20. `feature_flags.json: policy_engine.enabled = false` halts all new draft creation; manual revert and audit queries still work.
21. `oc policy disable` flips the flag atomically and writes an audit log entry.

**Daily budget (Addition #6):**

22. Daily budget caps at 3 changes per 24h regardless of recommendation count.
23. Pending drafts beyond budget are dropped with logging, not queued.

**Audit & UX:**

24. `/policy-changes --days 7` returns clean human-readable output with engine_version, reason, status, revert hook.
25. `policy_changes.hmac_self` chain validates via `verify_chain()` after every write.
26. `/policy-revert <id>` works at any state and writes a new HMAC chain link.
27. After 60 days of no further negative signal, a memory's `recall_penalty` decays to ≤0.05 (effectively neutral).

---

# What v0.5 Will Need (explicitly temporary v0 design choices)

These items are deliberately punted to v0.5 and **MUST be revisited.** This section exists so we don't forget that v0's simplifications are scaffolding.

## 1. Recommendation engine is dumb on purpose

`MostCitedBelowMedian/1` does not learn anything. v0.5 should ship at minimum:

- A regression-tree or simple model that uses Phase 1 features (composite signals + judge_score) to predict which memories are likely to underperform
- Distribution-shift detection (memory M's role in conversation has changed — don't penalize)
- Per-user calibration (memory M may be useful for topic A but noise for topic B)
- Counterfactual estimation (would the turn have scored higher *without* citing M?)
- Naming convention: `OutcomeRegressionTree/1`, `CounterfactualScorer/1`, etc., always with `recommendation_engine_version`

## 2. Explicit-approval default is a temporary scaffold

Once trust is established (10 successful decisions), TTL auto-approve takes over. But longer-term:

- **Tiered approval** by knob_kind (auto for low-blast knobs like `recall_penalty`, explicit for higher-blast knobs like skill priority or prompt variants)
- **Approval batching** (one Telegram per night with N recommendations, not N pings)
- **User-customizable approval policy** per knob_kind

## 3. Single knob

Phase 2 v0 modifies only `recall_penalty`. Future knobs (in priority order):

1. **Skill priority weight** — requires NEW infrastructure (skills today have no weight field; v0.5 must add skill weight schema)
2. **Tool consent friction** — per-tool, per-error-rate auto-tightening (UX-sensitive, needs careful design)
3. **Prompt variant selection** — A/B harness needed (major project)
4. **Model routing** — per-task-class auto-routing to cheap/expensive models

## 4. Snapshots are atomic

Pre-change baseline today captures full state via `snapshot/quick.py`. v0.5 should add per-knob selective restore so we don't bloat snapshot storage.

## 5. No A/B test harness

Can't yet test two recommendation engines side-by-side. v0.5 needs experiment cohorts with stable assignment.

## 6. Daily budget is global, not per-knob-kind

When v0.5 introduces second knob, budgets should be per-knob-kind so high-blast knobs are throttled tighter than low-blast.

## 7. No cross-knob coordination

When two knob-kinds exist, we need conflict resolution (what if `recall_penalty` change AND skill priority change target the same workflow?). v0.5 problem.

## 8. Trust ramp is global

Per-knob-kind trust counters in v0.5 (e.g., 10 safe `recall_penalty` decisions doesn't grant trust for `prompt_variant` decisions).

---

# Reward-Hacking Traps (explicit defenses)

| Trap | Defense |
|---|---|
| `tool_success_rate` alone → agent avoids tools | Composite weight 0.20, never standalone; LLM judge cross-checks "did agent attempt sufficient action?" |
| `reply_latency` low → terser less-useful answers | Latency only counts as friction when paired with negative vibe shift OR correction (not standalone) |
| `affirmation_present` → sycophancy | Weight capped at 0.10; cross-checked with action-completion proxy in judge prompt |
| `conversation_abandoned` → trailing meta-questions | Regex penalty for trailing "anything else?" in assistant final message; abandonment counts only when paired with prior friction |
| `vibe_drift` → aggressive cheerfulness | One of 8 signals; LLM judge calibrates against standing-order violations |
| `recall_value` → suppress inconvenient truths | `recall_penalty` is soft (floor 0.05); LLM judge ignores affect; suppression is auditable per memory |
| Engine games no-op threshold to hit budget | `minimum_deviation_threshold` prevents low-confidence recommendations; daily budget caps total volume |
| Engine recommends only safe-looking memories to inflate trust counter | Engine version is logged; v0.5 can compare cohorts; v0 mitigation is small action space (only +0.20, only on memories above eligibility threshold) |
| Engine and judge collude (same model bias) | Judge model must differ from agent model OR be a different family (e.g., Haiku judges Opus turns); v0 enforces this in `evals/providers.py` selection |

---

# Risk Register

| Risk | Severity | Mitigation |
|---|---|---|
| Honcho session-serialization broken by new observation writes | **High** | Per-session `asyncio.Lock`; stress test in P0 acceptance #4 |
| Phase 0 latency overhead | Medium | <50ms p99 target; benchmark required before P1 |
| Composite score mis-calibration | Medium | Judge cross-check; nightly disagreement audit |
| Cold-start: not enough data for statistical revert | Low | N=10 hard gate; status stays `pending_evaluation` |
| Auto-approve trust ramp gamed | Medium | Monitor unrevert decisions; engine version logged for cohort audit |
| User disables kill switch and forgets | Low | Kill switch is intentional; audit log shows when |
| Recall penalty cascades (memory → fewer citations → can't redeem) | Medium | Floor 0.05 ensures reachability; decay returns to neutral; explicit revert always available |
| Telegram notification spam in Phase A | Low | Approval batching deferred to v0.5; v0 sends one ping per recommendation but daily budget caps at 3 |

---

# Out of Scope (v0)

- Skill ranking / weighting (no infrastructure exists; defer to v0.5)
- Prompt variant A/B (no harness exists; major project)
- Tool consent auto-tightening (UX boundary)
- Multi-knob policy decisions
- User-facing approval UI beyond Telegram (web dashboard is v1+)
- Cross-session experiment cohorts (no harness exists)
- Fine-tuning on outcome signals (frozen weights via API by user constraint)
- Per-user trust ramps (single global counter in v0)

---

# Open Questions (deferred during spec phase, may resurface during planning)

1. Should `recall_synthesizer.py` apply penalty *before* the cheap-LLM synthesis step or only at retrieval time? (Lean: at retrieval; synthesis stays cheap and uses whatever survives ranking.)
2. Is 7-day `revert_after` TTL the right default for Phase B auto-approve? (Lean: yes, gives statistical revert ~10–15 eligible turns under typical usage.)
3. Should `safe_decision_counter` survive process restart? (Lean: yes; persisted in `feature_flags.json`.)
4. Should reverts be auto-applied when an active change is later contradicted by new evidence (e.g., post-mean drops below threshold *after* it had passed evaluation)? (Lean: yes — re-evaluation is continuous, not one-shot. The cron job re-checks `active` rows weekly.)
