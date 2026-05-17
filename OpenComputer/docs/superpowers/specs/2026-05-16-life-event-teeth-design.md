# Life-event teeth — design

Date: 2026-05-16
Owner: Saksham
Working dir: `/Users/saksham/Vscode/claude/OpenComputer`
Status: **v2 sub-project A** — follows the awareness-cleanup v1 plan (PR #625, merged). Sibling deferrals B (contradiction detector) and C (embedding relevance) get their own spec → plan → implement cycles later.

---

## 1. Context

The awareness-cleanup v1 work (M1–M5, merged in #625) delivered the three preconditions the plan named for this sub-project: clean data (`oc awareness migrate`), a context-aware reranker, and inspection tooling. PART-2 §219 of that plan: *"Once you have clean data, a working reranker, and visible inspection, then the E work becomes high-confidence rather than high-risk."* That condition is now met.

The life-events subsystem already exists and **detects** but does not **act**:

- `opencomputer/awareness/life_events/` — 6 pattern matchers (`burnout`, `exam_prep`, `health_event`, `job_change`, `relationship_shift`, `travel`) over a `LifeEventPattern` ABC, plus `registry.py` and `pattern.py`.
- Detection: `LifeEventRegistry` is a bus-subscribed singleton; every `SignalEvent` is dispatched to every non-muted pattern's `accumulate()`, which may return a `PatternFiring`.
- `PatternFiring` carries `surfacing: Literal["hint", "silent"]`, `hint_text: str`, `confidence`, `timestamp`. `"hint"` firings are appended to `registry._queue`; `"silent"` firings are not.
- `registry.drain_pending()` is the documented turn-start hook ("called by the chat surfacer at turn start"); `registry.peek_most_recent_firing()` already feeds the companion-persona overlay.
- `oc awareness patterns {list,mute,unmute}` already controls patterns; a muted pattern is skipped in `on_event`.

**The gap:** a `"hint"` firing produces `hint_text`, gets queued — and then nothing visible happens. This sub-project gives firings *teeth*.

---

## 2. Goal — "Done" in one sentence

A non-silent life-event firing injects a one-line hint + a tone directive into the next turn's prompt and auto-creates a gentle proactive check-in cron; a post-turn classifier autonomously cancels that cron when the user's response refutes the inference; `pytest` + `ruff` green.

---

## 3. Decisions (settled in brainstorming)

| Decision | Choice |
|---|---|
| What a firing does | **Hint + tone + proactive cron** — the full Approach E behaviour set. |
| Cron creation | **Auto-created on detection** (no upfront confirmation). |
| Cron correctness | **Self-correcting** — a post-turn classifier cancels the cron autonomously if the user's reply refutes the inference. Keep/cancel is an autonomous decision, not a user prompt. |
| Wiring approach | **Approach 3 — hook-driven + post-turn classifier.** Fully decoupled from `AgentLoop`; uses the hook system + a `DynamicInjectionProvider`. |
| Scope of "tone" | A lightweight per-event tone *directive* inside the injected hint — **not** a persona-registry overhaul. |

---

## 4. Architecture

Three decoupled pieces; no `AgentLoop` edits.

### 4.1 `LifeEventInjectionProvider`
`opencomputer/awareness/life_events/injection.py` — a `DynamicInjectionProvider` (the established per-turn injection pattern, cf. `path_rules_injection.py`). Each turn it drains the registry's pending `"hint"` firings and, for each non-muted firing, injects a block:

```
<life-event-hint>
{firing.hint_text}
{per-event tone directive — e.g. burnout: "Respond gently and concisely; don't pile on tasks."}
</life-event-hint>
```

Per-turn (not frozen-base) — a life-event detected mid-session must hint on the *next* turn. Muted patterns are skipped. Surfacing a firing's hint sets `verdict_pending = true` in state — the user's *next* reply is the one the classifier (§4.4) judges; the classifier clears the flag after rendering its verdict.

### 4.2 `life_event_actions`
`opencomputer/awareness/life_events/actions.py` — owns the cron follow-up lifecycle:
- `schedule_followup(firing)` — creates a cron via `opencomputer.cron.jobs.create_job`: a check-in job at a per-event delay (e.g. burnout → 3 days) that delivers a *gentle, no-pressure* message to the user's active channel.
- `cancel_followup(pattern_id)` — cancels the follow-up cron.
- **Dedup:** at most one active follow-up per `pattern_id`; a re-firing while one is active does not stack.

### 4.3 Hook wiring
- **Firing → cron:** when `registry.on_event` queues a non-silent firing, a `LifeEventFired` hook event is dispatched; a built-in handler calls `schedule_followup`. (Whether this reuses an existing hook event or adds `LifeEventFired` to `ALL_HOOK_EVENTS` is a pre-task — §7.)
- **Post-turn → classifier:** a `Stop` (post-turn) hook invokes the classifier (§4.4).

### 4.4 Post-turn classifier
`life_event_verdict` — runs **only when a follow-up is active and verdict-pending** (rare; not every turn). It examines the user's most recent message against the firing's hint and classifies `refuted` / `confirmed` / `unclear`:
- `refuted` → `cancel_followup(pattern_id)`.
- `confirmed` / `unclear` → keep the cron; clear the verdict-pending flag.

Implementation: a cheap heuristic pre-filter (obvious refutations — "I'm fine", "no, not stressed" — cancel with no inference); a small bounded LLM classification only for ambiguous replies. Bounded because it fires only while a verdict is pending.

### 4.5 State
`<profile>/life_event_state.json` — `{pattern_id: {firing_ts, cron_id, surfaced, verdict_pending}}`. Mirrors the existing per-profile knob files (`feature_flags.json`, `muted_patterns.json`). Atomic truncate-then-write; tolerates missing/corrupt (→ empty state).

### 4.6 CLI
Extends `oc awareness patterns`: a `status` subcommand shows active teeth (pattern, firing time, cron id, verdict-pending). `mute` / `unmute` already exist — a muted pattern produces no hint, no cron.

---

## 5. Data flow

```
bus SignalEvent → registry pattern.accumulate() → non-silent PatternFiring queued
  → LifeEventFired hook → schedule_followup(): cron created, state recorded
  → next turn: LifeEventInjectionProvider drains + injects <life-event-hint> (hint + tone)
  → agent acknowledges the life-event in its reply
  → user responds
  → Stop hook → classifier(user reply, firing)
       refuted          → cancel_followup() — false positive self-heals
       confirmed/unclear → keep; clear verdict_pending
  → (N days later) cron fires → gentle check-in message to the active channel
```

---

## 6. Error handling & risk

| Risk | Control |
|---|---|
| False-positive firing | Conservative pattern thresholds (already in place — patterns "rarely fire"); the post-turn classifier auto-cancels the cron; `oc awareness patterns mute` suppresses a pattern entirely. |
| False-positive cron survives (classifier misses) | The cron message is gentle and no-pressure; it fires once; it is visible and cancellable via `oc cron`. |
| Cron pile-up | One active follow-up per `pattern_id` (dedup in `actions`). |
| A hook / classifier crash | All hook + classifier paths fail-open — log at WARNING, never wedge the turn. A broken classifier leaves the cron in place rather than mis-cancelling. |
| Prompt bloat | One `<life-event-hint>` block, one line of hint + one line of tone; capped. |

---

## 7. Open questions / pre-tasks (gate implementation)

1. **Does `drain_pending()` have a live consumer today?** The registry docstring names a "chat surfacer" that drains at turn start, but a grep of `loop.py` did not find the call. Pre-task: confirm. If a consumer exists, `LifeEventInjectionProvider` must coordinate (not double-drain); if not, the provider becomes the sole consumer.
2. **Hook event for firings.** Is there a suitable existing hook event, or must `LifeEventFired` be added to `ALL_HOOK_EVENTS`? (CLAUDE.md notes `ALL_HOOK_EVENTS` is at 28.)
3. **Cron delivery target.** Confirm `cron.jobs.create_job` can target "the user's active channel" and deliver a free-text message.

If any pre-task contradicts the design, halt and revise before building.

---

## 8. Testing

- **Unit:** `LifeEventInjectionProvider` (firing → `<life-event-hint>` block; muted → skipped); `life_event_actions` (`schedule_followup` / `cancel_followup` / dedup); the classifier (`refuted` / `confirmed` / `unclear` over sample replies; heuristic pre-filter).
- **Integration:** firing → cron created → refuting reply → cron cancelled; firing → cron created → confirming reply → cron kept.
- **Regression:** the existing `oc awareness patterns` tests stay green; the companion-persona overlay's `peek_most_recent_firing` is unaffected.

---

## 9. Out of scope

- A persona-registry overhaul — tone is a directive in the hint, not a registry rewrite.
- Sub-project B (contradiction detector) and C (embedding relevance) — separate cycles.
- New life-event patterns, or surfacing policies beyond the existing `"hint"` / `"silent"`.
