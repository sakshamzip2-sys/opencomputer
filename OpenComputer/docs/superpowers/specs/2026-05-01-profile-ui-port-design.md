# Profile UI Port — Design (Plan 1 of 3)

**Date:** 2026-05-01
**Status:** Approved (verbal), proceeding to implementation plan.
**Author:** Claude (with Saksham's iterative guidance)
**Series:** Plan 1 of 3 — UI port → persona removal → auto-profile-suggester.

## Problem

The persona system (`opencomputer/awareness/personas/`) is being decommissioned. It is a 6-way auto-classifier that, in practice, drives a single binary decision in `prompts/base.j2` (`active_persona_id == "companion"` toggles three rule blocks). The other 5 personas produce the same rendered prompt as default. `disabled_capabilities` and `preferred_response_format` are dead code — no tool reads them. Profile is the strictly stronger abstraction (it owns memory, tools, model, system prompt, skills) and renders persona redundant.

But one piece of the persona system is genuinely valuable and worth preserving: the **`Ctrl+P` cycler + status-bar badge** in the TUI. It gives the user fast, ambient awareness of "which mode am I in" with a single keypress to change it. We want that affordance — applied to **profiles**, not personas.

This plan is **UI-only**. The persona auto-classifier keeps running internally (deleted in Plan 2). Slash commands `/persona-mode` keep working (deleted in Plan 2). The auto-profile-suggester is a separate feature (Plan 3).

## Approach

Port the persona Ctrl+P binding and badge rendering to the profile system. The profile switch takes effect at the **next user turn boundary** — current turn finishes with the current profile; the next prompt uses the new profile. No mid-turn `AgentLoop` swap, no restart required.

Decision rationale (option B from the brainstorm): mid-turn swapping evicts the prompt cache and produces a half-state where the model has been generating under one identity and finishes under another. Next-session swapping makes the gesture feel like configuration rather than control. Next-turn is the cleanest middle ground — instant feedback, atomic boundary, no cache eviction during streaming.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    Existing infrastructure                        │
│  • cli_ui/input_loop.py — PromptSession + key bindings            │
│    - Ctrl+P bound to _cycle_persona() at line 722                 │
│    - Badge renders at lines 424, 454, 483 reading active persona  │
│    - Hint text: "Shift+Tab mode · Ctrl+P persona"                 │
│  • opencomputer/profiles.py — profile resolution + list_profiles()│
│  • RuntimeContext.custom dict — mutable per-session state         │
│  • Existing /persona ensemble slash command                       │
│    (separate profile-dir switcher, distinct from /persona-mode)   │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│                       This plan                                   │
│  • Replace _cycle_persona → _cycle_profile in input_loop.py       │
│  • Replace persona badge → profile badge                          │
│  • Update hint text                                               │
│  • Add pending_profile_id state on RuntimeContext.custom          │
│  • Add turn-entry hook in agent/loop.py to consume pending state  │
│  • Reuse existing profile-load path (whatever /persona ensemble   │
│    uses) for the actual swap                                      │
└──────────────────────────────────────────────────────────────────┘
```

## Components

### 1. `_cycle_profile()` — replaces `_cycle_persona()` at `cli_ui/input_loop.py:722`

```python
def _cycle_profile(event):
    profiles = sorted(list_profile_names())   # from opencomputer.profiles
    if len(profiles) <= 1:
        # Single-profile state: nothing to cycle to. Show one-line hint.
        runtime.custom["profile_cycle_hint"] = "no other profiles — use /profile create"
        return

    current = runtime.custom.get("active_profile_id") or "default"
    pending = runtime.custom.get("pending_profile_id") or current
    idx = profiles.index(pending) if pending in profiles else 0
    next_profile = profiles[(idx + 1) % len(profiles)]

    runtime.custom["pending_profile_id"] = next_profile
    # Badge re-renders on next prompt-tick.
```

### 2. Badge rendering — replaces persona badge at `input_loop.py:424, :454, :483`

Format:
- No pending switch: `[D] mode: default · profile: coding`
- Pending switch: `[D] mode: default · profile: coding → work` (arrow indicates queued switch)
- Single profile only: `[D] mode: default · profile: default` (no cycling possible)

Color scheme inherits the existing persona badge styling (no visual change beyond the label).

### 3. Hint text — input footer

Change `Shift+Tab mode · Ctrl+P persona` → `Shift+Tab mode · Ctrl+P profile`.

### 4. Turn-entry hook — `agent/loop.py`

At the top of `run_conversation` (or wherever a single user-turn begins), before any model call:

```python
pending = runtime.custom.pop("pending_profile_id", None)
if pending and pending != _current_profile_id():
    _swap_profile(pending)   # reuse existing path
```

`_swap_profile` should reuse whatever code path the existing `/persona` ensemble slash command takes. The implementation plan will identify the exact entry point and confirm whether mid-session swap is supported by that path; if it isn't, the plan will scope the additional work needed (likely small — `AgentLoop` has all profile-bound state addressable through `MemoryManager`, `Config`, tool registry, system-prompt builder).

### 5. `/profile` slash command — confirm parity

If the existing `/profile` (or `/persona` ensemble) slash command does not already write `pending_profile_id`, add the write so slash and Ctrl+P share one path.

## Data flow

```
User presses Ctrl+P
   │
   ▼
_cycle_profile()
   │  • reads list_profile_names() from disk
   │  • computes next profile in sorted order
   │  • writes runtime.custom["pending_profile_id"] = next
   ▼
Badge re-renders to show "profile: current → next"
   │
   ▼  (user types prompt and submits)
Turn-entry hook in agent/loop.py
   │  • pops pending_profile_id
   │  • calls _swap_profile(new_id) — reuses /persona ensemble path
   │  • updates runtime.custom["active_profile_id"]
   ▼
Turn proceeds with new profile (new memory, tools, model, system prompt)
   │
   ▼
Badge re-renders to show "profile: new" (no pending arrow)
```

## Error handling

| Scenario | Behavior |
|---|---|
| User has only 1 profile (`default`) | Ctrl+P is a no-op. Sets `profile_cycle_hint` for one render-tick: "no other profiles — use /profile create". |
| Profile dir deleted between Ctrl+P and turn-entry | Turn-entry hook logs warning, clears pending, falls back to current profile. Next badge tick shows current. |
| Profile config (config.yaml or SOUL.md) fails to load | Swap aborts, current profile preserved, error surfaced in TUI as a one-line message. |
| `pending_profile_id` set but user never sends another turn | State persists across this session only (`runtime.custom` is per-session). Cleared at session end. Next session starts on whatever profile was active when the previous session ended. |
| User presses Ctrl+P multiple times before sending | Each press re-cycles. Final pending value wins. Badge always shows the latest pending value. |
| Concurrent /persona slash command + Ctrl+P | Both write the same `pending_profile_id` key. Last write wins. |

## Backwards compatibility

Plan 1 is intentionally **non-destructive of persona state**:

- The persona auto-classifier (`opencomputer/awareness/personas/classifier.py`) **keeps running**. Its output (`active_persona_id`) **keeps being injected** into the system prompt overlay. Removed in Plan 2.
- The persona badge rendering is **replaced**, not deleted — same line in `input_loop.py` now reads profile state.
- `/persona-mode` slash command **continues to work** but no longer drives the badge. Deleted in Plan 2.
- `/persona` ensemble slash command (the actual profile switcher) is **untouched**.
- `_persona_flips_in_session` counter and the `/profile-suggest` Learning Moment **continue to fire**. They will be re-anchored on profile flips in Plan 3.
- All existing tests in `tests/test_persona_classifier.py` (27 tests) **continue to pass**.

## Testing

| Test | Type |
|---|---|
| `_cycle_profile` cycles correctly with N≥2 profiles | Unit |
| `_cycle_profile` is a no-op with N=1, sets cycle hint | Unit |
| `_cycle_profile` cycles back to first after the last | Unit |
| Badge renders `profile: <id>` correctly | Unit |
| Badge renders `profile: <a> → <b>` when pending set | Unit |
| Hint text in input footer is `Ctrl+P profile` | Unit |
| Turn-entry hook reads + clears pending_profile_id | Unit |
| Turn-entry hook tolerates deleted profile (fallback) | Unit |
| Integration: Ctrl+P → next prompt → swap occurs | Integration |
| Integration: persona classifier still runs unchanged | Integration |
| Existing 27 persona-classifier tests | Regression |

Target: ≥10 new tests, all green. No existing tests change behavior.

## Out of scope (for this plan)

- Persona system removal (Plan 2)
- Auto-profile-suggester (Plan 3)
- Mid-turn `AgentLoop` swap (option C from brainstorm)
- Profile picker UI (we cycle, not pick)
- Profile creation from inside Ctrl+P (handled by `/profile create` slash command and the auto-suggester in Plan 3)
- Cross-session pending persistence (`pending_profile_id` is per-session only)
- Gateway / Telegram parity — Ctrl+P is a TUI-only key binding; remote channels switch profile via slash command (already supported)

## Risks / open questions

1. **Existing profile-swap mechanism may not support mid-session swap.** The `/persona` ensemble slash command exists but its actual swap behavior hasn't been verified by this plan. If it does next-session swap only, the implementation plan needs to either: (a) extend it to support mid-session swap, or (b) accept that "next user turn" actually means "next session" and update the spec. Mitigation: implementation plan's first task is to read the existing path and confirm. Estimated 30 min of investigation; either outcome is recoverable.

2. **Profile-bound state surface area.** Swapping a profile mid-session means swapping `MemoryManager` (different `MEMORY.md`/`USER.md`), `Config` (different model/tool config), tool registry filter (different enabled plugins), `prompt_builder` source (different SOUL.md / system prompt), and possibly the session DB (`sessions.db` is per-profile). Some of this is straightforward; the session DB swap is the riskiest — mid-session swap may invalidate session-id-bound state. Mitigation: implementation plan opens a fresh session-id on swap (treat the post-swap turn as a new logical session within the same TUI process); existing pre-swap session is preserved on disk under the old profile.

3. **Phase 1 of profile-as-agent-multi-routing dependency.** The current branch (`feat/profile-as-agent-phase-1`) is doing `ProfileContext` ContextVar plumbing. Plan 1 here lives on the same branch. The plumbing is no-behavior-change so this plan does not depend on it logically, but the merge order matters. Mitigation: rebase on `feat/profile-as-agent-phase-1` if it merges first; otherwise stack Plan 1 on top.

## Estimated size

~150–250 LOC including tests. ~1 day of implementation + audit + ship.
