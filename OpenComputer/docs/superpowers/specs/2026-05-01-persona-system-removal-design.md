# Persona System Removal — Design (Plan 2 of 3)

**Date:** 2026-05-01
**Status:** Approved (verbal, after self-audit), proceeding to implementation plan.
**Author:** Claude (with Saksham's iterative guidance + audit ritual)
**Series:** Plan 2 of 3 — UI port (DONE) → persona removal (this) → auto-profile-suggester.

## Problem

The persona system (`opencomputer/awareness/personas/`) is over-engineered for the work it actually does. Six personas, a 174-LOC regex auto-classifier with hysteresis and stability gates, prompt-cache eviction logic, and ~30 tests — all serving a single binary toggle in `prompts/base.j2` (the three rule-block conditionals at `:4, :36, :52` only fire for `active_persona_id == "companion"`). The other 5 personas produce the same rendered prompt as default. `disabled_capabilities` and `preferred_response_format` in the YAMLs are dead code — no caller ever reads them.

Plan 1 ported the user-facing affordance (Ctrl+P + status badge) from persona to profile. The persona auto-classifier still runs internally but no longer drives the UI. Plan 2 removes the auto-classifier and all its scaffolding. Plan 3 (separate spec) will rebuild a smarter auto-profile-suggester on a cleaner foundation.

## Approach: Option A — pure deletion + UX preservation

Delete the entire persona system. The single piece of behavioral value the system delivered (companion register on social messages) is preserved by making `companion.yaml`'s register guidance **unconditional** in `base.j2` — the modern Claude model handles register adaptation natively from a clear universal instruction.

**What stays the same for the user:**
- Ctrl+P still cycles profiles, badge still shows profile (Plan 1).
- The agent still adapts register: technical/crisp on coding messages, warm/companion on social messages. The mechanism changes; the behavior doesn't.
- `vibe_classifier` still runs (regex, separate system, harmless).
- Life events still accumulate in the F4 graph silently for observability + Plan 3 to read.

**What changes for the user:**
- `/persona-mode` slash command goes away. (Replaced functionally by Ctrl+P from Plan 1.)
- `/profile-suggest` slash command goes away. (Plan 3 brings back a better version.)
- The Learning Moment that nudged "you keep flipping personas → try /profile-suggest" goes away. (Plan 3 brings back better signals.)
- `oc awareness personas list` CLI subcommand goes away. (`oc awareness patterns` for life events stays.)

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    BEFORE (current state)                       │
│                                                                 │
│  Per-turn classifier (174 LOC regex + hysteresis)               │
│         │                                                       │
│         ▼                                                       │
│  runtime.custom["active_persona_id"]                            │
│         │                                                       │
│         ▼                                                       │
│  prompt_builder ──▶ base.j2 conditionals ──▶ companion overlay  │
│                                                                 │
│  Side branches:                                                 │
│   • _persona_flips_in_session counter ──▶ /profile-suggest LM   │
│   • profile_analysis re-classifies past sessions                │
│   • cli_awareness personas list                                 │
│   • Life-event chat surfacer (companion-only gate)              │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                    AFTER (Plan 2 end state)                     │
│                                                                 │
│  base.j2 always renders register-adaptation prelude             │
│  (companion.yaml substance + technical-mode rules together,     │
│  model picks per-message)                                       │
│                                                                 │
│  Side branches: ALL DELETED                                     │
│   • profile_analysis.py and tests                               │
│   • /profile-suggest slash command + LM predicate               │
│   • cli_awareness personas subcommand                           │
│   • Life-event chat surfacer (registry keeps accumulating       │
│     silently to F4)                                             │
└─────────────────────────────────────────────────────────────────┘
```

## Inventory — what gets touched

### Production files — DELETE entirely

| Path | LOC | Reason |
|---|---|---|
| `opencomputer/awareness/personas/__init__.py` | small | module index |
| `opencomputer/awareness/personas/classifier.py` | 174 | regex auto-classifier |
| `opencomputer/awareness/personas/registry.py` | small | YAML loader |
| `opencomputer/awareness/personas/_foreground.py` | small | helper |
| `opencomputer/awareness/personas/defaults/coding.yaml` | ~10 | persona overlay |
| `opencomputer/awareness/personas/defaults/companion.yaml` | 102 | substance moves to `base.j2` |
| `opencomputer/awareness/personas/defaults/trading.yaml` | ~10 | overlay |
| `opencomputer/awareness/personas/defaults/learning.yaml` | ~10 | overlay |
| `opencomputer/awareness/personas/defaults/admin.yaml` | ~10 | overlay |
| `opencomputer/awareness/personas/defaults/relaxed.yaml` | ~10 | overlay |
| `opencomputer/agent/slash_commands_impl/persona_mode_cmd.py` | ~100 | `/persona-mode` slash |
| `opencomputer/profile_analysis.py` | ~200 | depends on `classify()`; Plan 3 rebuilds |
| `opencomputer/agent/slash_commands_impl/profile_suggest_cmd.py` | ~100 | depends on `profile_analysis` |

### Production files — REFACTOR (delete persona-specific code, keep file)

| Path | What to delete | What stays |
|---|---|---|
| `opencomputer/cli_awareness.py` | `personas_app` Typer group + `personas_list()` (~25 LOC, lines 27-29 + 119-149) | `patterns_app` group (life events) + module skeleton |
| `opencomputer/agent/loop.py` | `_active_persona_id` attr (`:371`), `_persona_flips_in_session` (`:381`), `_reclassify_calls_since_flip`, `_maybe_reclassify_persona` method, `_build_persona_overlay` method, all callers (~400 LOC across `:872, :1391, :1725-1741, :1823-1830, :2109-2223`) | Everything else |
| `opencomputer/agent/prompt_builder.py` | `active_persona_id`, `persona_overlay`, `persona_preferred_tone` parameters and Jinja context plumbing (~30 LOC across `:182, :201, :237, :259, :280, :380, :412`) | Everything else |
| `opencomputer/agent/prompts/base.j2` | The 3 conditional rule blocks at `:4, :36, :52` + the `## Active persona` overlay rendering at `:223-234` (~30 LOC) | Replace with unconditional register-adaptation prelude (~120 LOC — see below) |
| `opencomputer/cli_ui/input_loop.py` | Orphan `_cycle_persona` (`:376-402`), persona docstring refs (`:464-465`), any remaining `active_persona_id` reads (`:454, :483` if still present after Plan 1) | Everything else |
| `opencomputer/agent/learning_moments/predicates.py` | `suggest_profile_suggest_command` predicate (`:375`) — depends on `_persona_flips_in_session` | Other predicates |
| `extensions/affect-injection/provider.py` | Update docstring at `:100` that references `_build_persona_overlay` | Behavior unchanged (docstring-only) |

### `runtime.custom` keys — DELETE all

`active_persona_id`, `persona_id_override`, `_persona_dirty`, `_persona_flips_in_session`. Plus any leftover plumbing.

### Tests — explicit per-file action

| Path | Action | Notes |
|---|---|---|
| `tests/test_persona_classifier.py` (27 tests) | **Delete** | classifier gone |
| `tests/test_persona_loop_integration.py` | **Delete** | tests `_maybe_reclassify_persona`, hysteresis, override flow |
| `tests/test_persona_mode_command.py` | **Delete** | slash command gone |
| `tests/test_persona_registry.py` | **Delete** | registry gone |
| `tests/test_profile_analysis.py` | **Delete** | profile_analysis.py gone |
| `tests/test_companion_persona.py` | **Rewrite** | tests base.j2 conditionals → rewrite to assert "system prompt always contains register-adaptation prelude (companion-register substance)" |
| `tests/test_companion_anti_robot_cosplay.py` | **Rewrite** | tests anti-"As an AI" warmth-padding behavior — UX I want preserved → rewrite to assert these rules in the unconditional prelude |
| `tests/test_companion_life_event_hook.py` | **Rewrite** | tests companion-only life-event chat surfacing → rewrite to assert "life events accumulate to F4 graph regardless; chat surface path removed" |
| `tests/test_vibe_log.py` | **Patch** | drop persona column from vibe-log entries |
| `tests/test_learning_moments.py:940` | **Delete** | `test_suggest_profile_suggest_fires_on_three_persona_flips_default_profile` — LM gone |
| `tests/test_mode_badge.py` (6 xfailed) | **Delete** | strict-xfail will XPASS once persona reads gone — delete the tests, not unmark |

So: **6 test files deleted entirely + 4 rewritten/patched + 1 single-test deletion**. ~30 tests touched.

## `base.j2` strategy — substance preservation

The current state is "3 conditionals fire for non-companion personas; companion is the gentler default that turns them off." The new state is "all turns get a single prelude that teaches the model to adapt per-message."

The unconditional prelude has three blocks:

### Block 1 — register adaptation directive (top-of-prompt, ~10 lines)

```
You adapt your register to the user's. The same agent talks to the same
user across very different moments — sometimes shipping a PR, sometimes
asking how you are. Match what the message actually asks for, and pivot
when they pivot.

- Technical / task / coding messages: be concise, action-biased, no
  warmth padding, no hedging, declarative sentences. Code first.
- State-query / personal / social messages: use the companion register
  in the next section. Anchored honesty, not performance.
```

### Block 2 — companion register guidance (verbatim from `companion.yaml`'s `system_prompt_overlay`, ~80 lines)

The full content of the YAML overlay — the two failure modes, the three lanes (companion / reflective / warm-neutral), the 8 hard rules, the "why this register exists" reasoning. **This is content that proved its UX value across PR #163 and Companion Voice work; we keep it verbatim, just unconditional.**

### Block 3 — keep existing always-on prelude as before (~30 lines)

The base.j2 already has system identity, available tools, etc. that aren't persona-conditional. These stay.

**Net base.j2 size**: was ~250 lines with the conditionals + overlay rendering. After: ~340 lines unconditional. **+90 LOC, but cache-prefix is hit on every turn after the first** so per-turn cost is unchanged.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Companion warmth regresses on social messages without explicit persona signal | The new unconditional prelude includes companion.yaml verbatim; modern Claude (4.6/4.7) routes register from message content reliably. Tests `test_companion_persona.py`, `test_companion_anti_robot_cosplay.py` (rewritten) are regression coverage. |
| Technical concision regresses on coding messages because companion content is always in prompt | The Block 1 directive explicitly tells the model to be concise/action-biased on technical messages. We're trusting Claude to discriminate; this is what modern instruction-following models are built for. |
| Mid-session prompt cache invalidation increases | Should DECREASE — no more persona flips evicting `_prompt_snapshots[sid]`. base.j2 grows by ~90 lines but is part of the cached prefix on every turn. |
| `/profile-suggest` and the persona-flips Learning Moment go dark | Plan 3 rebuilds. User-pull commands have no scheduled dependency; LMs are best-effort discovery. Acceptable gap. |
| Life events stop surfacing to chat | Documented limitation; registry keeps accumulating to F4 graph for Plan 3 to consume. Not a regression in observability, only in chat-side UX. |
| `oc awareness personas list` users confused by command removal | Out-of-scope for chat surface; CLI users are technical. Surface in CHANGELOG. |
| Refactor breaks unrelated tests | The 4 companion-related tests are explicitly rewritten as regression coverage. Full pytest suite must pass before merge. |

## Out of scope (for Plan 2)

- Auto-profile-suggester (Plan 3)
- Persona-as-profile auto-switching (Plan 3)
- Re-surfacing life events to chat (deferred — Plan 3 may revisit)
- Renaming the ensemble `/persona` slash command to `/profile use` (separate cleanup, not blocking)
- Anything in `extensions/` beyond the affect-injection docstring touch

## Estimated size

- **Deletions**: ~750-900 LOC across 13 production files + 6 test files
- **Rewrites**: ~30 lines net add in base.j2 (companion content moves in unconditionally) + ~80 lines of test rewrites
- **Net**: roughly **-700 LOC**, big simplification
- **Commits**: ~12-15 logical chunks, each independently revertable
- **PR size**: 1 day of implementation + audit

## Commit ordering (preview — full plan in next step)

1. Delete `awareness/personas/` module (classifier + registry + 6 YAML defaults)
2. Delete `cli_awareness.py` personas subcommand (keep patterns)
3. Delete `/persona-mode` slash command + tests
4. Strip `agent/loop.py` persona machinery (the heaviest commit; ~400 LOC delta)
5. Strip `agent/prompt_builder.py` persona params
6. Replace `base.j2` conditionals with unconditional prelude (incorporates companion.yaml substance)
7. Delete `_cycle_persona` orphan + persona docstring refs in `input_loop.py`
8. Delete `profile_analysis.py` + `/profile-suggest` slash command + LM predicate
9. Rewrite the 4 companion/vibe regression tests
10. Delete the 6 test files (classifier, loop integration, mode command, registry, profile_analysis, single LM test)
11. Delete the 6 xfailed tests in `test_mode_badge.py`
12. Update `extensions/affect-injection/provider.py` docstring
13. Smoke + ruff sweep + full pytest

## Self-review (audit-driven)

Five fixes from the self-audit, all applied here:

1. **`cli_awareness.py` personas subcommand** — added to inventory.
2. **Test plan made explicit** — 6 deletes + 4 rewrites + 1 patch (`vibe_log`) + 1 single-test deletion in `test_learning_moments`.
3. **`base.j2` substance preservation** — companion.yaml's `system_prompt_overlay` content moves verbatim into the unconditional prelude. Not a 1-sentence "match register."
4. **Companion regression tests rewritten, not deleted** — `test_companion_persona`, `test_companion_anti_robot_cosplay`, `test_companion_life_event_hook` become regression coverage for the unconditional prelude + life-events-to-graph behavior.
5. **Honest size estimate** — 750-900 LOC removed, 30 tests touched (not "27").
