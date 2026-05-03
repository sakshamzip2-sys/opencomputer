# Outcome-Aware Learning — Runbook

**What it does:** OpenComputer records implicit signals on every turn (tool success/failure, user reply latency, affirmation/correction regex hits, abandonment, vibe drift), fuses them into a per-turn `turn_score`, and uses that score to drive ONE reversible policy knob: a soft decaying `recall_penalty` on under-performing memories. No explicit user feedback required.

**Spec:** [`docs/superpowers/specs/2026-05-03-outcome-aware-learning-design.md`](../superpowers/specs/2026-05-03-outcome-aware-learning-design.md)

---

## Architecture (3 phases)

| Phase | What | User-visible |
|---|---|---|
| **0. Passive recording** | `turn_outcomes` rows + `recall_citations` linkage on every turn | No |
| **1. Outcome scoring** | composite + LLM-judge → fused `turn_score` | Dashboard only |
| **2. v0 adaptive loop** | reversible `recall_penalty` knob with HMAC-audited `policy_changes`, progressive trust ramp, statistical revert, kill switch, daily budget | Yes (`/policy-changes`, Telegram pings) |

---

## Enabling / disabling

```bash
oc policy enable      # turn the recommendation engine ON
oc policy disable     # kill switch — halts new drafts; manual revert still works
oc policy status      # show current state
```

Persistent flags live at `~/.opencomputer/<profile>/feature_flags.json`. Edit by hand or via CLI; values match the spec defaults:

```json
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
```

---

## Inspecting changes

```bash
oc policy show                    # last 7 days (default)
oc policy show --days 30          # last 30 days
```

Or in chat:

```
/policy-changes
/policy-changes --days 14
```

Output rows show: timestamp, short id, knob, target, status, approval_mode, engine_version, reason.

---

## Approving + reverting

### Phase A — explicit approval (default for first 10 decisions)

Every recommendation lands as `pending_approval`. Telegram admin (if `TELEGRAM_ADMIN_CHAT_ID` is set) gets a DM with the change details and a `/policy-approve <short-id>` hint.

```
/policy-approve abcdef12
```

After 10 successful unrevert decisions, the engine transitions to Phase B:

### Phase B — TTL auto-approve

Recommendations apply automatically with a 7-day evaluation window. Statistical auto-revert (N≥10 eligible turns + post-mean below baseline-1σ) fires within that window if outcomes degrade. Telegram notification sent on auto-apply; another DM if a revert later fires.

### Manual revert

```
/policy-revert <short-id>
```

Works at any state except already-reverted. Sets `recall_penalty` back to its previous value (typically 0.0), marks status as `reverted`, writes a new chain link with `reverted_reason="user-initiated /policy-revert"`.

---

## Reading the audit chain

Every `policy_changes` row carries an HMAC chain link (`hmac_prev` → `hmac_self`). Tampering with any row's content (knob_kind, target_id, prev/new value, reason, expected_effect, rollback_hook, engine_version, approval_mode) breaks the chain.

To verify the chain manually:

```python
from opencomputer.agent.config import _home
from opencomputer.agent.config_store import default_config
from opencomputer.agent.policy_audit import PolicyAuditLogger
from opencomputer.agent.policy_audit_key import get_policy_audit_hmac_key
from opencomputer.agent.state import SessionDB

cfg = default_config()
db = SessionDB(cfg.session.db_path)
key = get_policy_audit_hmac_key(_home())

with db._connect() as conn:
    log = PolicyAuditLogger(conn, key)
    print("chain valid:", log.verify_chain())
```

If `verify_chain()` returns False, something has tampered with the policy_changes table or the HMAC key has changed.

---

## Status lifecycle

```
            engine_tick
                │
                ▼
            drafted
                │
        ┌───────┴───────┐
   Phase A          Phase B
   (explicit)       (auto_ttl)
        │               │
        ▼               ▼
pending_approval   pending_evaluation
  │       │             │
  │   /policy-approve   │
  │       │             ▼
  │       └─→ pending_evaluation
  │                     │
  │            auto_revert + decay_sweep run
  │                     │
  ▼                     ▼
  expired_decayed (7d auto-discard)
                  │
                  ├─→ active (within ±1σ)
                  │     │
                  │     ▼ (decays over ~60d)
                  │   expired_decayed
                  │
                  └─→ reverted (post < baseline - 1σ)
```

Manual `/policy-revert` works at any state except `reverted`.

---

## Troubleshooting

### "I want to roll back without waiting for statistical revert"

`/policy-revert <short-id>` from a chat surface, or run the SQL manually:

```bash
sqlite3 ~/.opencomputer/<profile>/sessions.db
> UPDATE episodic_events SET recall_penalty = 0.0,
>   recall_penalty_updated_at = strftime('%s', 'now')
>   WHERE id = <ep_id>;
> UPDATE policy_changes SET status = 'reverted',
>   reverted_at = strftime('%s', 'now'),
>   reverted_reason = 'manual sql rollback'
>   WHERE id = '<change_id>';
```

(The chain remains valid because UPDATEs of `status`/`reverted_*` are not chain-protected — they're audit-logged but not cryptographically sealed in v0.)

### "Engine seems frozen — no new drafts"

Check in order:

```bash
oc policy status                              # is enabled True?
                                              # safe_decisions_so_far + phase
sqlite3 ~/.opencomputer/<profile>/sessions.db
> SELECT COUNT(*) FROM turn_outcomes;          # at least 14d of data?
> SELECT COUNT(*) FROM recall_citations;        # citations recorded?
> SELECT COUNT(*) FROM policy_changes
>   WHERE ts_drafted > strftime('%s','now') - 86400;  # daily budget hit?
```

If `daily_change_budget` is exhausted (default 3 changes per 24h), the cron skips. If the corpus has too few citations or all candidates are within `minimum_deviation_threshold` of corpus median, the engine is correctly emitting no-ops.

### "I want to nuke everything and start over"

```bash
oc policy disable
sqlite3 ~/.opencomputer/<profile>/sessions.db
> UPDATE episodic_events SET recall_penalty = 0.0,
>   recall_penalty_updated_at = NULL;
> DELETE FROM policy_changes;        -- breaks the chain — re-init
                                      -- happens on next engine_tick
> DELETE FROM turn_outcomes;          -- nuclear option; loses all P0/P1 data
> DELETE FROM recall_citations;
oc policy enable
```

---

## Tunables (persisted in feature_flags.json)

| Flag | Default | What it controls |
|---|---|---|
| `enabled` | `true` | Master kill switch |
| `auto_approve_after_n_safe_decisions` | `10` | Phase A → Phase B threshold |
| `daily_change_budget` | `3` | Max applied changes per 24h |
| `min_eligible_turns_for_revert` | `10` | Hard gate: never auto-revert below this many post-change turns |
| `revert_threshold_sigma` | `1.0` | Std-deviations below baseline that trigger auto-revert |
| `decay_factor_per_day` | `0.95` | Per-day exponential decay on `recall_penalty` |
| `minimum_deviation_threshold` | `0.10` | Engine no-op if no candidate's mean is more than this below corpus median |

---

## What v0.5 will need

These are deliberately deferred from v0 (see spec §9 for full discussion):

- Smarter recommendation engine (`OutcomeRegressionTree/1` learning from Phase 1 features instead of dumb-heuristic ranking)
- Tiered approval (auto for low-blast knobs, explicit for higher-blast)
- Approval batching (one Telegram per night, not N pings)
- Skill priority weighting (skills today have no weight field — needs new infrastructure)
- Prompt-variant A/B harness
- Cryptographic chain extension on status transitions (currently chain protects as-drafted only)
- Selective snapshot restore (atomic-only today)
- Cross-knob coordination
- Per-user trust ramps
- Per-knob-kind daily budgets
- Recommendation engine quality meta-metric (% recommendations surviving to expired_decayed)
