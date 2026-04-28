# Prompt A — Cross-persona vibe consumption — Contract

**Date:** 2026-04-28
**Branch:** `feat/affect-work-abc` (worktree at `~/.config/superpowers/worktrees/claude/affect-work`)
**Status:** Implemented + tested. 11/11 vibe_log tests pass; 108/108 in the persona/vibe/awareness/learning_moments scope pass.

---

## What changed

Two pre-existing pieces, plus one new piece in this prompt:

### 1. Vibe classification site (already generalised before this prompt)

**Location:** `opencomputer/agent/loop.py:1273-1282` inside `AgentLoop._build_persona_overlay()`.

**State on entry to this prompt:** vibe classification was already lifted out of the companion-only branch by an earlier change (PR #205, comment at `loop.py:1259-1269`: *"Path A.4 (2026-04-27, generalised 2026-04-28): vibe classification runs on EVERY user turn regardless of active persona"*). The verdict is persisted to BOTH:

- `sessions.vibe` (most-recent only) via `SessionDB.set_session_vibe()` at `state.py:595-606`.
- `vibe_log` (per-row, with `classifier_version`) via `SessionDB.record_vibe()` at `state.py:608-643`.

This part was a no-op for this prompt — already shipped.

### 2. Cross-session anchor injection (the actual delta of Prompt A)

**Location before this prompt:** `loop.py:1311-1355`, inside `if result.persona_id == "companion":`. Non-companion personas got no cross-session continuity.

**Location after this prompt:** `loop.py:1311-1379` (line numbers shifted by +24 due to the framing block), at function-body indent — outside the companion-only `if`. It runs on every persona.

### 3. Persona-aware framing of the cross-session anchor

When `prev` is non-None and the gating condition (next section) passes:

| Persona | Framing | Heading | Body |
|---------|---------|---------|------|
| `"companion"` | reflective, asks the user to acknowledge the shift | `## PREVIOUS-SESSION VIBE (anchor for the companion)` | Existing text, unchanged: *"User's apparent emotional state in their last different session ({age_str}, '{title}'): **{vibe}**. If the user's tone now is markedly different, you can naturally reference the shift — 'you sounded {vibe} last we talked, this feels different — what changed?'. Don't force it; use only when the contrast is obvious."* |
| any other (`"coding"`, `"trading"`, `"relaxed"`, `"learning"`, `"admin"`, etc.) | neutral, background-context only | `## Recent user state` | *"User's apparent emotional state in their last different session ({age_str}): **{vibe}**. Useful background context only — don't reference it explicitly unless the current turn makes the contrast genuinely relevant."* |

`age_str` is `"{N}h ago"` when ≥ 1 hour or `"less than an hour ago"` otherwise. The companion variant additionally surfaces the previous session's `title`.

### 4. Signal gate (calm-skip)

**Rule:** When `prev["vibe"] == "calm"` (or empty / falsey), no anchor is injected for ANY persona.

**Reason:** `"calm"` is the regex classifier's default fallback at `vibe_classifier.py:118-119`. A "recent user state: calm" anchor is effectively "we have no signal on the user's mood" — pure noise. Worth re-thinking once a confidence-scored backend ships (e.g., embedding-based or LLM-judged), but for the regex pipeline today, calm == no-signal.

**Code:** `loop.py:1335` — the guard `if prev is not None and prev.get("vibe") and prev.get("vibe") != "calm":`.

---

## Pre-LLM chain step where vibe is now classified

Per the audit-03-pipeline.md numbering, vibe classification still happens during step 7 (Memory reads / frozen-base contributors), inside `_build_persona_overlay()` which is invoked once per session at `loop.py:621`. **This is "once per session", not "once per turn"** — see Gotchas.

The cross-session anchor injection is also part of step 7 — it's appended to the persona overlay string before the prompt builder freezes it into the base prompt.

---

## Per-persona framing rules (canonical for downstream prompts)

```
if persona == "companion":
    heading = "## PREVIOUS-SESSION VIBE (anchor for the companion)"
    body    = <reflective, can-reference shift if obvious>
else:
    heading = "## Recent user state"
    body    = <neutral, background only, don't reference unless relevant>
```

Both variants are skipped entirely when `prev["vibe"] == "calm"` or no `prev` row exists within the 72h window.

---

## What is guaranteed unchanged

- `vibe_classifier.classify_vibe()` — the regex implementation is untouched. Same 6-label vocabulary (`frustrated|excited|tired|curious|calm|stuck`), same priority order (stuck > frustrated > excited > tired > curious > calm fallback), same `RegexClassifier` with `FIRST_MATCH` policy.
- The `sessions.db` schema — no migrations, no new columns. `vibe`, `vibe_updated`, and `vibe_log` already existed.
- The companion-only "RECENT LIFE EVENT" anchor at `loop.py:1284-1310` — STAYS in `if result.persona_id == "companion":`. Prompt A explicitly does not touch life-event injection.
- The persona auto-classifier at `awareness/personas/classifier.py` — untouched.
- The companion overlay's existing "PREVIOUS-SESSION VIBE (anchor for the companion)" framing string is byte-for-byte identical when rendered (verified by `test_prev_session_anchor_companion_keeps_existing_framing`).
- `vibe_log` write semantics — same row shape, same `classifier_version="regex_v1"`.

---

## Tests added

`tests/test_vibe_log.py` (3 new tests, lines 188-291):

- `test_prev_session_anchor_companion_keeps_existing_framing` — companion sees the existing reflective framing.
- `test_prev_session_anchor_non_companion_uses_neutral_framing` — coding persona sees `## Recent user state` block, not the companion-specific text.
- `test_prev_session_anchor_skipped_when_prev_vibe_calm` — calm-gate verified for both companion and non-companion paths.

Plus two helpers: `_seed_prev_session_with_vibe()` (back-dates `vibe_updated` to a known age) and `_run_overlay_with_persona()` (mocks the persona classifier + foreground app + vibe classifier).

---

## Files touched

| File | Change |
|------|--------|
| `opencomputer/agent/loop.py` | Replaced lines 1306-1356 (companion-only previous-session-vibe block) with a function-body-indented block carrying persona-aware framing + calm gate. Net: same behaviour for companion + new behaviour for non-companion + skip on calm. |
| `tests/test_vibe_log.py` | Added 3 tests + 2 helpers. |

No new files. No CLI changes. No schema changes. No prompt_builder.py changes (the anchor is built in `_build_persona_overlay()` which feeds into the existing `persona_overlay` template variable).

---

## Gotchas

### Gotcha 1: "every user turn" is aspirational, actually "once per session"

The comment at `loop.py:1259-1269` says vibe classification runs *"on EVERY user turn"*, but `_build_persona_overlay()` is called only at `loop.py:621` — once per session, in the same lane as `user_facts` / `workspace_context`, so the resulting overlay lands on the FROZEN base prompt and prefix cache stays warm. As a consequence:

- `vibe_log` accumulates one row per session, not one per user turn.
- `sessions.vibe` is set once at session start, not updated mid-session.

**Why this is OK for now:**
- Prompt A's contract is about cross-persona behaviour, not cadence. Fixing cadence would mean moving the vibe block out of the persona overlay path entirely (into the per-turn pre-LLM chain proper) AND would invalidate the prefix cache on every turn, costing tokens.
- Prompt B's affect-injection plugin runs as a `DynamicInjectionProvider`, which IS per-turn. Per-turn vibe will land there.

**Why a downstream prompt might care:**
- Prompt B should NOT assume `vibe_log` has a current-turn row. It can read `sessions.vibe` (set once at session start) and the most recent few `vibe_log` rows for the session, but the current message itself may not be reflected yet.

### Gotcha 2: The `if last_user_messages:` guard inside the vibe block

`loop.py:1273` guards vibe classification on `if last_user_messages:`. On a brand-new session with zero user messages, vibe is not classified — `sessions.vibe` and `vibe_log` stay empty. That's fine for any current consumer (the cross-session anchor lookup explicitly filters out the current session anyway, and falls through to no-anchor when no prior session has a vibe). Prompt B should be aware that `sessions.vibe` may be NULL on the current turn.

### Gotcha 3: Companion life-event anchor is unaffected by this prompt

The companion-only "RECENT LIFE EVENT" block at `loop.py:1284-1310` was NOT touched. Life-event firings still inject only on the companion persona path. If non-companion personas should also get life-event hints, that is a separate scope (Prompt B's `<user-state>` block has `active_pattern` which addresses this through a different mechanism).

---

## Behavioural delta summary (for Prompt B's reader)

| Before | After |
|--------|-------|
| Companion turns get `## PREVIOUS-SESSION VIBE (anchor for the companion)` block when prev vibe within 72h existed (any value, even calm). | Companion turns get the same block UNLESS prev vibe was calm (signal-gate added). |
| Non-companion turns get nothing cross-session. | Non-companion turns get `## Recent user state` block when prev vibe within 72h existed AND was non-calm. |
| Non-companion turns: per-session vibe was already classified, persisted to sessions.vibe + vibe_log. | Unchanged. |

---

## Handoff to Prompt B

Prompt B can rely on:

1. The cross-session anchor block exists on every persona (with the appropriate framing) and signal-gates on calm.
2. The current-session vibe is set on `sessions.vibe` at session start (once per session, NOT per turn).
3. The `vibe_log` table accumulates verdicts by `classifier_version`; current shipped backend is `regex_v1`.
4. The 6-label vocabulary is the only output.
5. `LifeEventRegistry.peek_most_recent_firing()` is available and non-destructive (per `awareness/life_events/registry.py:80`).
6. Prompt B's `<user-state>` block is **complementary** to the cross-session anchor: anchor describes "what was the user carrying in" (cross-session, frozen at session start); `<user-state>` describes "what state are they in right now" (per-turn, mutated by `DynamicInjectionProvider.collect()`).
7. Both can coexist in the system prompt without duplication, because the anchor lands in the FROZEN base via persona overlay, while `<user-state>` lands in the per-turn deltas via injection. They go through different rendering paths.

