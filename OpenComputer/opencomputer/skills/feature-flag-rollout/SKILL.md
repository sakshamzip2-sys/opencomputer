---
name: feature-flag-rollout
description: Use when designing a feature flag, planning canary rollout, gradual exposure, or kill switches
---

# Feature Flag Rollout

## When to use

- Risky feature shipping behind a switch
- Needing per-user / per-tenant rollout control
- Adding a kill switch for a known fragile path

## Steps

1. **Default off.** New flags ship disabled in all environments. Promote intentionally.
2. **Naming.** `feat_<area>_<thing>` (e.g. `feat_billing_invoice_v2`). Prefix tells the reader it's a flag.
3. **Bucketing strategy.**
   - Random %: `random()` per request. Cheap, statistically sound.
   - Sticky %: hash of user id. Same user always gets same answer. Required for UI flags.
   - Allowlist: explicit user/tenant ids. Use for beta partners.
4. **Three states, not two.** `off` / `canary` / `on`. Canary = some %; `on` = 100%; cleanup = remove the flag (don't leave dead conditionals forever).
5. **Telemetry on both sides.** Track success/error rate for `flag=on` AND `flag=off` separately. Promotion criteria = "no worse than control."
6. **Kill switch SLA.** If you need to flip the flag off, how long does it take? <30s is the target. Cache invalidation is the usual blocker.
7. **Cleanup burndown.** Old flags accumulate dead code. Add a "remove by date" tag in the flag config.

## Notes

- Don't use feature flags for permissions / auth. That's an authorization concern, different system.
- Avoid flag-of-flags. If two flags interact, refactor to one combined flag.
- A/B testing is a different discipline; don't conflate it with feature flagging (though they share infrastructure).
