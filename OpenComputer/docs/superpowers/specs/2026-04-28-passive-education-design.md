# Passive Education ("Learning Moments") — design spec

**Date:** 2026-04-28
**Status:** Final after Round 1 (expert-critic) + Round 2 (adversarial) audits
**Goal:** Solve OpenComputer's discoverability gap — users don't know what the agent can do — without lecturing, interrogating, or interrupting. Indirect, contextual, low-frequency reveals modeled on therapist + skilled-communicator patterns.

---

## Problem

A non-technical user opens `oc chat`, types a few messages, gets useful answers — and never discovers vibe tracking, memory continuity, life-event detection, the user-model graph, the cost guard, or any of the 25+ subsystems quietly working under the hood. Today's only education path is "read the README." That's a 3% solution.

## Anti-patterns we are NOT shipping

- "Did you know..." tips
- Tutorial walkthroughs
- Modal popups on first run
- Daily-tip emails / banners
- Yes/no question chains
- Feature-of-the-day spam

## Six design principles

1. **Default to silence.** Reveals don't fire unless a deterministic trigger matches AND cap not hit AND moment not previously fired.
2. **One reveal per UTC-day, hard cap.** Max 3 per week.
3. **Always inline-tail, never modal.** Italic dim line at the end of the assistant turn.
4. **Demonstrate, then optionally name.** First the agent uses the feature; later it surfaces what just happened.
5. **Triggered by user behavior, not by schedule.**
6. **Persist what's been taught — never repeat.**

## Architecture

`opencomputer/awareness/learning_moments/` package:

| Module | Purpose |
|---|---|
| `registry.py` | Hand-curated `LearningMoment` list |
| `predicates.py` | Cheap trigger functions (O(1) on hot path) |
| `engine.py` | `select_next_moment(ctx)` + cap enforcement |
| `store.py` | JSON read/write at `~/.opencomputer/<profile>/learning_moments.json`, file-locked |
| `surface.py` | Inline-tail formatting (v1); placeholders for system-prompt-overlay (B) and session-end (C) |

Hook point: `opencomputer/agent/loop.py` post-turn.

CLI: `oc memory learning-off` / `learning-on` / `learning-status`.

## Moments shipped in v1 (3, intentionally small)

1. `memory_continuity_first_recall` — user references a fact already in MEMORY.md.
2. `vibe_first_nonneutral` — first non-calm vibe in a session.
3. `recent_files_paste` — user types a path-like string.

3 more candidates (`cross_session_recall`, `user_md_unfilled`, `confused_session`) deferred to v2.

## Cap & severity

- Per-day cap = 1, weekly cap = 3, both UTC.
- `severity = "tip"` (suppressible) | `severity = "load_bearing"` (always fires).
- `learning-off` only suppresses `tip` severity. Load-bearing prompts (e.g. PR #209's smart-fallback) keep firing.

## Returning-user seed

If `learning_moments.json` is absent AND `sessions.db.sessions` shows >5 prior sessions on first call, seed all moments as `fired_once_at = <epoch>` so returning users don't get a noise burst.

## Audit findings rolled in

### Round 1 (expert critic) — 15 issues

Load-bearing fixes:
- Per-day cap, not per-session
- Cheap predicates only on hot path
- Pre-flight data verification before referencing
- Tail-clause format for streaming UX
- Shared persistence with companion-overlay reveals
- First-ever reveal includes opt-out hint
- CLI-only in v1
- `oc help tour` as separate opt-in command (later)

### Round 2 (adversarial) — alternatives compared

| Approach | Naturalness | Predictability | LLM-drift risk |
|---|---|---|---|
| **A — Inline tail clause** ⭐ v1 | low–med | very high | none |
| B — System-prompt overlay | very high | low | medium |
| C — Session-end reflection | high | high | low |

v1 picks **A** for highest predictability + easy rollback. Registry shape leaves room for B and C in v2 (each moment can declare its preferred surface).

### Worst-case edge cases handled

- Wipe of `~/.opencomputer/` → returning-user seed prevents re-onboarding.
- Clock / TZ skew → all caps in UTC, documented.
- Concurrent `oc chat` sessions → file-locked write; same-day double-fire is acceptable degradation.
- Moment references a feature that doesn't exist → `min_oc_version` field on moment.
- Critical-path reveals → `severity` field separates tips from load-bearing.

### Quantified uncertainty

- ~85% the user wants ≥1 of the 3 v1 moments
- ~60% at least one v1 predicate is mistuned and either over-fires or never fires (instrumentation will tell us)
- ~50% we'll want to switch ≥1 moment to surface B or C in v2

## What v2 adds (NOT in this PR)

- Mechanism B (system-prompt overlay) for soft signals
- Mechanism C (session-end reflection)
- LLM-level soft triggers via cheap-route
- Empty-state pass on 5 CLI commands
- Better failure-message text on 3 error paths
- `oc help tour` opt-in guided experience
- Telegram / Discord per-platform templates
- 3 more curated moments

## Done definition (v1)

- Predicate evaluation runs in <1ms per turn (instrumented)
- Each of the 3 moments fires correctly on a synthetic session that triggers it
- Each moment fires AT MOST once per profile, EVER
- Cap enforces ≤1 reveal per UTC-day, ≤3 per UTC-week
- `oc memory learning-off` → no tip reveals fire; load-bearing still fire
- Returning-user seed prevents reveal burst on a fresh `learning_moments.json` with prior sessions in the DB
- All tests pass; ruff clean
