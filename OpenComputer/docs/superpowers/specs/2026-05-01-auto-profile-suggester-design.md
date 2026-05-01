# Auto-Profile-Suggester — Design (Plan 3 of 3)

**Date:** 2026-05-01
**Status:** Approved (verbal), proceeding to implementation plan.
**Author:** Claude (with Saksham's iterative guidance)
**Series:** Plan 3 of 3 — UI port (DONE) → persona-removal (decided to keep V2) → auto-profile-suggester (this).

## Problem

OpenComputer supports profiles (per-profile memory, tools, model, system prompt) but the only way to discover that a new profile would help is for the user to notice it themselves and run `oc profile create`. The existing `/profile-suggest` slash command exists but is **user-pull** (you have to know to type it). For users who code by day and side-project by night, the system has all the data needed to notice that pattern — it just doesn't proactively surface it.

Saksham's vision (verbatim from chat 2026-05-01): "create a system in which the profile can be auto-chosen or a recommendation goes to the user to create his own profile based on what he's trying to do... if he's working throughout the day and doing a side project in the night, then we can suggest him during the daytime to create a work profile and keep the side project profile as default... we can do something like this so that it helps, it kind of pushes the user to just say yes and the profile gets automatically created."

## Approach

Three additive components on top of existing infrastructure:

1. **Pattern-detection module** — extends `profile_analysis.compute_profile_suggestions()` with two new signals: time-of-day clusters and cwd clusters. Writes a cached suggestion file to `~/.opencomputer/profile_analysis_cache.json`.

2. **Background scheduler (OS-level cron)** — `oc profile analyze install` writes a launchd plist (macOS) or systemd user timer (Linux) that runs `oc profile analyze` daily at 9am local. Cross-platform fallback: Windows falls back to "lazy-on-startup-if-stale" since neither launchd nor systemd is available.

3. **Proactive surfacing + auto-creation** — the existing `suggest_profile_suggest_command` Learning Moment is upgraded to read the cache file and fire when there's a fresh, actionable, non-dismissed suggestion. New `/profile-suggest accept <name>` slash subcommand that programmatically creates the profile, seeds a tailored SOUL.md based on the detected pattern, and confirms.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ Existing infrastructure (reused, not rewritten)                  │
│  • profile_analysis.compute_profile_suggestions()                │
│  • /profile-suggest slash command (user-pull, kept)              │
│  • suggest_profile_suggest_command Learning Moment predicate     │
│  • SessionDB (per-session metadata: cwd, started_at, persona)    │
│  • Persona V2 classifier (8-signal Bayesian combiner)            │
│  • profiles.real_user_home() (HOME-mutation-immune)              │
│  • profiles.create_profile(name, clone_from=...)                 │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ NEW Plan 3 components                                            │
│                                                                  │
│  profile_analysis_daily.py (NEW — pattern detector)              │
│    • compute_daily_suggestions(db) -> list[DailySuggestion]     │
│    • Time-of-day clusterer with HIGH-confidence gate             │
│    • cwd clusterer                                                │
│    • Combines with existing persona signal                       │
│    • Writes ~/.opencomputer/profile_analysis_cache.json          │
│                                                                  │
│  cli_profile_analyze.py (NEW — `oc profile analyze` group)       │
│    • run         — manual daily analysis                          │
│    • install     — launchd (macOS) / systemd (Linux)             │
│    • uninstall   — remove cron                                    │
│    • status      — installed?, last-run, next-run                 │
│                                                                  │
│  scheduler/ (NEW — OS-specific install templates)                │
│    • launchd_template.plist                                      │
│    • systemd_timer_template / systemd_service_template           │
│    • Reuses existing systemd-install pattern from cli.py:2389    │
│                                                                  │
│  slash_commands_impl/profile_suggest_cmd.py (UPGRADED)           │
│    • New subcommand: /profile-suggest accept <name>              │
│    • New subcommand: /profile-suggest dismiss <name>             │
│    • Existing analysis output: unchanged                         │
│                                                                  │
│  learning_moments/predicates.py (UPGRADED)                       │
│    • suggest_profile_suggest_command also reads cache            │
│    • Fires when fresh + actionable + non-dismissed               │
│                                                                  │
│  soul_seeder.py (NEW — tailored SOUL.md generator)               │
│    • render_seeded_soul(suggestion) -> str                       │
│    • Templates by detected persona (coding/trading/etc.)         │
└──────────────────────────────────────────────────────────────────┘
```

## Detection signals (V1 scope)

| Signal | Source | Status | Confidence gate |
|---|---|---|---|
| Persona-classification clusters | Existing `profile_analysis.py` | Reused | ≥3 sessions in last 30 (existing) |
| Time-of-day clusters | NEW in `profile_analysis_daily.py` | New | **≥70% of sessions in a 4-hour band over 30+ sessions** (high-confidence gate) |
| cwd clusters | NEW in `profile_analysis_daily.py` | New | ≥40% of sessions in a single cwd or sibling directory tree, over 30+ sessions |
| foreground-app | Persona V2 already uses per-session | Skipped (V2) | Depends on opt-in sensor; silent absence is unfriendly |
| LLM topic clustering | None | Skipped (V2) | Cost + complexity not justified for V1 |

## High-confidence gates (load-bearing — addresses brittleness concern)

The biggest risk in this design is **false-positive suggestions** ("you should have a Wednesday-2pm profile" — nonsense). Mitigations:

1. **Cold-start gate**: Suggestions never fire if the user has fewer than **10 sessions in the analysis window**. Fresh users see nothing until enough data accumulates.

2. **Pattern-strength gate (time-of-day)**: A time cluster must have ≥70% of analyzed sessions in a single 4-hour band. A user with bimodal usage (9am-1pm AND 8pm-11pm) will fire two distinct cluster suggestions if both individually clear 70% of their respective bands.

3. **Pattern-strength gate (cwd)**: ≥40% of sessions in one directory subtree. Lower than time-of-day because cwd is a stronger signal when present (explicit user choice of where to start `oc`).

4. **Persona overlap gate**: A persona-cluster suggestion only fires if the persona doesn't already match an existing profile name (existing logic in `_persona_matches_profile`).

## Data flow

```
Daily 9am (cron)
  │
  ▼
oc profile analyze run
  │
  ├─► SessionDB → recent 30 sessions
  ├─► For each session: re-classify persona (V2), bin by time-of-day, bin by cwd
  ├─► Apply high-confidence gates
  ├─► Generate DailySuggestion[] for clusters that pass gates
  ├─► Read previous cache for dismissed list
  ├─► Filter out dismissed-within-7-days
  ▼
~/.opencomputer/profile_analysis_cache.json
  {
    "last_run": "2026-05-02T09:00:00Z",
    "suggestions": [
      {"kind": "create", "name": "work", "rationale": "...", "command": "..."},
      {"kind": "create", "name": "side-project", "rationale": "...", "command": "..."}
    ],
    "dismissed": [{"name": "personal", "until": "2026-05-09T09:00:00Z"}]
  }


Next time user starts `oc`
  │
  ▼
LM check (suggest_profile_suggest_command predicate)
  │
  ├─► Read cache (or skip if missing/stale)
  ├─► Filter dismissed
  ├─► First fresh suggestion → fire LM
  ▼
User sees:
  💡 Profile suggestion (from yesterday's analysis):
  You've coded 18 of your last 30 sessions, typically 9am-6pm.
  A dedicated 'work' profile would keep your code memory separate.
    Accept:  /profile-suggest accept work
    Dismiss: /profile-suggest dismiss work


User types /profile-suggest accept work
  │
  ├─► profiles.create_profile("work")  (existing)
  ├─► soul_seeder.render_seeded_soul(suggestion) → SOUL.md content
  ├─► Write to ~/.opencomputer/profiles/work/SOUL.md
  ├─► Update cache: remove this suggestion
  ▼
User sees:
  ✅ Profile 'work' created with seeded SOUL.md.
     Switch to it: Ctrl+P  (or restart with `oc -p work`)
```

## Dismissal semantics

User runs `/profile-suggest dismiss <name>` → that specific suggestion (by name) is suppressed for **7 days**. The cache stores `{"name": <name>, "until": <ts+7d>}`. After 7 days, if the pattern still holds, suggestion re-fires (giving the user another chance to reconsider).

Dismissing one suggestion does NOT suppress others — if the user dismisses "work" but "side-project" is also pending, "side-project" still fires.

A "stop all suggestions" mode is **out of scope for V1** — if a user wants to fully disable, they uninstall the cron via `oc profile analyze uninstall`.

## Seeded SOUL.md

`soul_seeder.render_seeded_soul(suggestion)` produces a tailored opening based on the detected persona + pattern. Examples:

**Coding pattern**:
```
You are the work-mode agent for {user_name}.
Focus: software engineering and shipping work tasks.

Detected pattern: 18 of last 30 sessions classified as coding work,
typically 9am-6pm. Frequent directories: ~/Vscode/work/, ~/Code/.

Be technical, action-oriented, code-first. Drop warmth padding for
task-focused requests. Default to 1-4 sentences when answering
technical questions; show code over describing it.
```

**Trading pattern**:
```
You are the trading-mode agent for {user_name}.
Focus: stock market analysis and investment decisions.

Detected pattern: 12 of last 30 sessions classified as trading,
typically when TradingView/Zerodha was foreground.

Always cite live data over cached. Flag when something has already
been priced in. Be brief on price targets, generous on rationale.
```

The user can edit the seeded SOUL.md after creation. The seeder produces a starting point, not a fixed fact.

## OS-level scheduler (B from brainstorm — accepted with reservations)

| Platform | Mechanism | File location |
|---|---|---|
| macOS | launchd plist | `~/Library/LaunchAgents/com.opencomputer.profile-analyze.plist` |
| Linux | systemd user timer + service | `~/.config/systemd/user/opencomputer-profile-analyze.{timer,service}` |
| Windows | Not yet supported | Print message that user should run `oc profile analyze` manually; consider Task Scheduler in V2 |

Mirror the pattern from `cli.py:2389` (`oc service install` / `oc service uninstall`) for consistency. Existing systemd-install code in `opencomputer/service.py` is the template — extend with launchd parallel implementation.

**Note on B vs A trade-off**: I (Claude) flagged that B is more code than A (lazy-on-startup-if-stale) for marginal benefit. User explicitly chose B knowing this. The implementation plan will include a lazy-fallback path that fires if the cron isn't installed, so the system still works for users who never run `oc profile analyze install`.

## Onboarding integration

During the existing `oc setup` wizard (verify path before implementing — see Risk #4), prompt:

```
? Run a daily background pass to suggest profiles based on your
  usage patterns? (Y/n) [Y]
```

If yes → automatically run `oc profile analyze install` for the platform. If `oc setup` doesn't have a clean integration point, fall back to: print a one-time hint after the user accepts the first suggestion, telling them to run the install command if they want it daily.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Time-of-day false positives for irregular schedules | High-confidence gate (≥70% in a 4-hour band over 30+ sessions). Documented in user-facing rationale. |
| `oc setup` integration may not have a clean prompt insertion point | Implementation plan's first task is to read `oc setup` flow and decide. Fallback: post-first-suggestion hint. |
| launchd plist format unverified | Implementation plan includes a smoke test that loads the plist via `launchctl bootstrap` and verifies it runs. |
| Cold-start gives nothing | <10 sessions → suggestions don't fire. Acceptable; LM stays silent until enough data accumulates. |
| User dismisses 1 suggestion, expects all to stop | Documented: dismiss is per-name. "Stop all" requires `oc profile analyze uninstall`. |
| B is over-engineered for V1 | Code includes lazy-fallback so users who never install the cron still get suggestions on `oc` startup. B's only marginal benefit (suggestions fresh while you're on vacation) is acknowledged but not load-bearing. |
| `~/.opencomputer/profile_analysis_cache.json` corruption | Lazy-fallback recomputes; cache is treated as best-effort, not authoritative. |

## Out of scope (V2 if Plan 3 V1 ships)

- foreground-app pattern detection (depends on opt-in sensor most users haven't enabled)
- LLM topic clustering (cost + complexity)
- Auto-routing via `bindings.yaml` (rejected during brainstorm — too brittle for human schedules)
- Cross-profile suggestions ("you have 3 profiles but pattern Z suggests merging two of them")
- Windows Task Scheduler integration

## Estimated size

- `profile_analysis_daily.py` — ~200 LOC (added time-of-day + cwd binning, cache I/O)
- `cli_profile_analyze.py` (new typer subapp) — ~150 LOC
- `scheduler/launchd.py` (new) + `scheduler/systemd.py` (extends existing) — ~250 LOC
- `soul_seeder.py` (new) — ~120 LOC
- LM upgrade in `learning_moments/predicates.py` — ~50 LOC
- Slash command `/profile-suggest accept|dismiss` extension — ~100 LOC
- Tests — ~400 LOC across 5-6 test files

**Total: ~1,300 LOC, ~8 commits, 2 days of focused work.** (Up from my original 800 estimate after self-audit revealed launchd is more code than I claimed.)
