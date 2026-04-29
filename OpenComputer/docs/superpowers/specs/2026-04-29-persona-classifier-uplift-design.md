# Persona Classifier Uplift — Design Spec

**Date:** 2026-04-29
**Author:** archit-2 session (Saksham + Claude Opus 4.7)
**Status:** Draft for user review

## Background

`opencomputer/awareness/personas/classifier.py` selects one of six YAML overlays
(`admin` / `coding` / `companion` / `learning` / `relaxed` / `trading`) at session
start, and the result is stamped into the frozen base prompt via
`AgentLoop._build_persona_overlay` (`loop.py:1532`) once, inside the
`_prompt_snapshots` cache build at `loop.py:761`.

The classifier was deliberately excluded from the recent
`RegexClassifier` abstraction (`docs/superpowers/plans/2026-04-28-regex-classifier-abstraction.md`):

> *Migrating persona classifier — its compound logic (foreground app +
> state-query + file extensions + time of day) doesn't cleanly fit
> `RegexClassifier`. Stays as-is until a richer abstraction lands.*

Production session evidence shows three concrete problems:

1. **Multi-line first-message defeats the state-query check.** The
   regex at `classifier.py:43-54` is anchored at start-of-string. When a
   user pastes a venv-activate command followed by `hi`, the message
   reads `source ...\nhi\nhello` — the regex matches `source`, fails,
   and rule #4 (`coding` from `iTerm`) fires. The user is greeted as
   coding-mode despite a clear social opener.
2. **No mid-session adaptation.** The classifier runs once at session
   start. `vibe_classifier` already runs every user turn (the loop
   comment at `loop.py:1641-1648` confirms it). Persona is the only
   personalisation signal that's frozen — asymmetrically with vibe.
3. **No mid-session override.** A user observing a wrong classification
   has no in-session knob. `/persona` belongs to the ensemble profile
   switcher (`opencomputer/ensemble/persona_command.py`), and
   `/personality` is a storage-only knob with a different vocabulary
   (`helpful`/`concise`/`technical`/...). Neither reaches the
   auto-classifier overlay.

Additional minor gaps observed:

- **English-only state-query patterns.** "kaise ho", "kya haal hai", and
  Hinglish openers don't fire — relevant for an `en_IN` user.
- **No emotion lexicon.** Messages like *"i am sad just went through a
  break up"* contain no greeting marker but are overwhelmingly
  companion-shaped; the classifier has no rule for "user is talking
  about feelings" and falls through to time-of-day fallback.

## Goal

Make the persona classifier:

1. Correct on multi-line / non-English / emotion-leading first messages.
2. Adaptive within a session — re-classify per user turn with a
   stability gate (no flapping).
3. User-overridable — a `/persona-mode <id>` slash command that wins
   over the classifier until explicitly cleared.

Preserve every existing invariant:

- Defensive at every layer (any failure → empty overlay → never break
  startup).
- Prefix-cache friendly when persona is stable (no cache thrash).
- The 6 YAML overlays themselves are unchanged.
- The existing `_prompt_snapshots` shape is unchanged; we evict the
  snapshot on persona flip rather than re-architecting prompt assembly.

## Out of scope (deferred)

- LLM-based classifier backend. Park until heuristics + adaptation
  prove insufficient.
- New personas (a "general" persona between `admin` and `companion`
  may be warranted, but adding a persona changes prompt-assembly
  conditionals — separate PR).
- Per-user learning loop ("user said switch to X" → labelled signal).
  Needs a corpus first.
- Renaming `/persona` (ensemble profile switching) for namespace
  cleanup. Out of scope.
- Migrating the persona classifier to `RegexClassifier`. Its compound
  logic (foreground app + state-query + file-extension count + time
  of day) doesn't cleanly fit. The richer abstraction (compound
  classifier with weighted multi-signal aggregation) is a separate
  effort; this PR ships the behavior fixes on the current shape.

## Architecture

Three additive changes, no refactors:

### 1. Bug fixes inside `classifier.py`

- **Per-line state-query check.** When the latest user message contains
  newlines, split on `\n` and run `is_state_query` against each line.
  Match if any line matches. Same for the prior 2 messages.
- **Emotion-lexicon rule.** New rule fires if the latest message
  contains an emotion-anchor term (`sad`, `lonely`, `happy`, `stressed`,
  `excited`, `tired`, `heartbroken`, `grieving`, `frustrated`, etc.).
  Inserted between trading/relaxed (which are explicit user-app
  choices and should still win) and the coding-app rule.
- **Hindi/Hinglish state-query patterns.** Add `kaise\s+ho`,
  `kaisa\s+hai`, `kya\s+haal`, `kya\s+chal`, `theek\s+ho`, `sab\s+badhiya`
  to the state-query regex.

These are pure behavior fixes; no shape change to `ClassificationContext`
or `ClassificationResult`.

### 2. Per-turn re-classification with stability gate

- New method `AgentLoop._maybe_reclassify_persona(session_id)`. Called
  on every user turn AFTER the user message is persisted (so the
  classifier sees it).
- Builds the same `ClassificationContext` as the session-start path.
- Compares `result.persona_id` to `self._active_persona_id`.
- **Stability gate:** the new persona must match for 2 consecutive
  turns before we flip. Tracked via `self._pending_persona_id` and
  `self._pending_persona_count`. Confidence ≥ 0.85 short-circuits the
  gate (strong app signal flips immediately).
- On a confirmed flip: evict `self._prompt_snapshots[sid]`, update
  `self._active_persona_id` and `_active_persona_preferred_tone`,
  mirror into `runtime.custom["active_persona_id"]`. Next turn
  rebuilds the snapshot with the new persona overlay. Log the flip
  at `INFO` level for observability.
- **Override-locked.** If `runtime.custom["persona_id_override"]` is
  set, skip re-classification entirely; the override sticks.
- **Cooldown / cost note.** Re-classification cost is dominated by
  `detect_frontmost_app()`'s 2-second osascript timeout. Cache the
  foreground app value for 30 seconds inside the loop instance to avoid
  spawning a subprocess on every turn. (Vibe classifier already runs
  per turn without app detection, so this is the only added subprocess
  work.)

### 3. `/persona-mode` slash command

New slash command at
`opencomputer/agent/slash_commands_impl/persona_mode_cmd.py`:

```
/persona-mode                → list available personas + show active
/persona-mode <id>           → override classifier; set active persona
/persona-mode auto           → clear override; re-enable classifier
```

Storage: `runtime.custom["persona_id_override"]` (string, empty/missing
== no override). Validation: must be one of the registered persona ids
from `list_personas()`. Forces an immediate `_prompt_snapshots` evict
for the session so the new persona takes effect on the very next turn.

`_build_persona_overlay` is updated to read the override before
running the classifier:

```python
override = self._runtime.custom.get("persona_id_override", "") if self._runtime else ""
if override:
    persona = get_persona(override)
    if persona is not None:
        # use override path; skip classifier
        ...
        return overlay
# else: classifier path (unchanged)
```

The slash command is registered alongside the existing
`SkinCommand` / `PersonalityCommand` registrations. The command is
intentionally distinct from `/persona` (ensemble profile switching)
and `/personality` (knob with a different vocabulary).

## Data flow (single session)

```
session start
    ↓
_build_persona_overlay (once, inside snapshot build)
    ├─ if override set → use override
    └─ else → classify(ctx) → overlay
    ↓
prompt snapshot built, persona stamped in
    ↓
user turn N
    ↓
user message persisted
    ↓
_maybe_reclassify_persona(sid)
    ├─ if override set → return (no-op)
    └─ else:
       ├─ ctx = build_context()  (foreground cached for 30s)
       ├─ result = classify(ctx)
       ├─ if result.persona_id == active → reset pending counter, return
       ├─ if result.persona_id == pending → pending_count += 1
       │                                  → on count >= 2 OR conf >= 0.85:
       │                                       evict snapshot, update active
       └─ else → pending = result.persona_id, pending_count = 1
    ↓
prompt build for turn N
    ├─ snapshot exists → reuse
    └─ snapshot evicted (persona flipped) → rebuild with new overlay
```

## Failure modes — every layer must degrade safely

| Layer | Failure | Behavior |
|------|--------|---------|
| `is_state_query` per-line split | message is non-string / None | catch in caller, fall through (current behavior) |
| Hindi pattern compile | regex syntax error in source | caught at module import → fail loud at dev time, not runtime |
| Emotion lexicon match | empty messages list | already handled by `last_msg = ctx.last_messages[-1] if ctx.last_messages else ""` |
| `_maybe_reclassify_persona` | any exception | log at debug; leave `_active_persona_id` unchanged; no snapshot evict |
| Override path in `_build_persona_overlay` | override id refers to deleted persona | `get_persona` returns None → fall through to classifier |
| `/persona-mode <bad_id>` | unknown id | command returns error string; runtime unchanged |
| `/persona-mode auto` after explicit override | override clear | `runtime.custom.pop("persona_id_override", None)`; force snapshot evict so the next turn re-classifies |

The non-negotiable contract from the existing code stays in force: **a
classifier or override failure must NEVER break agent startup or a
turn**.

## Tests

New tests in `tests/`:

- `test_persona_classifier.py` (extend existing):
  - `test_multi_line_first_message_state_query_matches`
  - `test_emotion_anchor_message_classifies_companion`
  - `test_hindi_state_query_classifies_companion`
  - `test_classifier_uses_all_three_recent_messages`
- `test_persona_loop_integration.py` (extend existing):
  - `test_reclassify_flips_persona_after_stability_gate`
  - `test_reclassify_does_not_flap_on_single_signal`
  - `test_reclassify_skipped_when_override_set`
  - `test_reclassify_evicts_prompt_snapshot_on_flip`
  - `test_reclassify_high_confidence_short_circuits_gate`
- `test_persona_mode_command.py` (new):
  - `test_persona_mode_lists_personas_and_active`
  - `test_persona_mode_sets_override`
  - `test_persona_mode_auto_clears_override`
  - `test_persona_mode_rejects_unknown_id`

All existing tests in `test_persona_classifier.py`,
`test_persona_loop_integration.py`, and `test_persona_registry.py`
must continue to pass unchanged.

## Telemetry

Log lines (at `INFO` level by default; classifier is otherwise quiet):

```
persona_classifier.flip session=<sid> from=<old> to=<new> reason=<rule>
persona_classifier.override_set session=<sid> id=<id>
persona_classifier.override_clear session=<sid>
```

These hit the existing `_log` channel in `loop.py`. No new sinks.

## Migration / rollout

The change is purely additive. Sessions that started before the change
continue to work because:

- The classifier returns the same shape and same persona ids.
- The override path is a no-op when `runtime.custom["persona_id_override"]`
  is absent (default).
- The re-classification path is opt-in by code-path (called every turn,
  but its only side effect is a snapshot evict that the existing LRU
  already does).

No config migration, no flag, no opt-out needed. If a regression appears,
the rollback is the inverse of the diff (no schema involved).

## Acceptance criteria

A user who:

1. Starts a session with `source .venv/bin/activate\nhi\nhello` as the
   first message → classifier picks `companion`, not `coding`.
2. Starts in coding mode then writes "i am sad just went through a
   break up" → after one more emotion-shaped turn, persona flips to
   `companion`. The next turn renders the companion overlay.
3. Runs `/persona-mode companion` → next turn renders companion overlay
   regardless of foreground app or messages.
4. Runs `/persona-mode auto` → classifier resumes; override cleared.
5. Runs `/persona-mode invalid` → command rejects with the list of
   valid ids; runtime unchanged.

All five paths covered by tests.
