# Life-Event Teeth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire life-event detections to behaviour — inject a one-line hint + tone directive into the next turn's prompt and auto-create a self-correcting check-in cron.

**Architecture:** Approach 3 — fully decoupled from `AgentLoop`. A `DynamicInjectionProvider` surfaces the hint per-turn and creates the cron at first surface; a `STOP`-hook classifier autonomously cancels the cron when the user's reply refutes the inference.

**Tech Stack:** Python 3.13, pytest, ruff. OC `plugin_sdk` (`DynamicInjectionProvider`, `HookEvent`), `opencomputer.cron.jobs.create_job`, `opencomputer.awareness.life_events`.

**Spec:** `docs/superpowers/specs/2026-05-16-life-event-teeth-design.md` — read it first.

---

## Pre-flight (resolved during planning — recorded for the implementer)

- **`registry.drain_pending()` has no live consumer today.** The life-event "chat surfacer" was never wired (`loop.py` only `peek`s, for the persona overlay). `LifeEventInjectionProvider` becomes the sole drainer — Task 1 first decouples `peek` so the overlay survives the drain.
- **Teeth apply to the 4 `surfacing="hint"` patterns** — `JobChange`, `ExamPrep`, `Burnout`, `Travel`. `HealthEvent` and `RelationshipShift` are `surfacing="silent"` and are NOT queued — untouched by this plan.
- **No firing hook event exists.** Rather than add a public `HookEvent`, the injection provider creates the cron at first-surface (functionally "on detection" — the firing surfaces on the very next turn). Only the classifier uses a hook (`STOP`).
- **`create_job`** (`opencomputer/cron/jobs.py`) is keyword-only: `schedule: str`, `prompt`, `name`, `notify`, `repeat`, `origin_platform/chat_id/thread_id`, … Returns a dict with the job id.

## File Structure

| File | Responsibility |
|---|---|
| `opencomputer/awareness/life_events/registry.py` (modify) | Add `_last_firing` so `peek_most_recent_firing` survives a queue drain. |
| `opencomputer/awareness/life_events/state.py` (create) | Load/save `<profile>/life_event_state.json` — active teeth state. |
| `opencomputer/awareness/life_events/actions.py` (create) | Cron follow-up lifecycle — `schedule_followup` / `cancel_followup`, dedup. |
| `opencomputer/awareness/life_events/injection.py` (create) | `LifeEventInjectionProvider` — per-turn hint+tone injection; creates the cron at first surface. |
| `opencomputer/awareness/life_events/classifier.py` (create) | `classify_response()` — refuted / confirmed / unclear; the `STOP`-hook handler. |
| `opencomputer/cli_awareness.py` (modify) | `oc awareness patterns status` subcommand. |
| `opencomputer/agent/loop.py` or the provider registry (modify) | Register `LifeEventInjectionProvider` + the `STOP` hook handler. |
| `docs/awareness/life-events.md` (create) | User + dev doc. |

---

## Task 1: Decouple `peek_most_recent_firing` from the drained queue

**Files:**
- Modify: `opencomputer/awareness/life_events/registry.py`
- Test: `tests/test_life_events_registry.py`

The injection provider will `drain_pending()` every turn. The companion-persona overlay (`loop.py:4166`) calls `peek_most_recent_firing()`, which currently reads `_queue` — after a drain it would return `None`. Decouple them.

- [ ] **Step 1: Write the failing test**
```python
def test_peek_survives_drain():
    reg = LifeEventRegistry()
    reg._queue.append(PatternFiring(pattern_id="burnout", confidence=0.8,
                                    evidence_count=3, surfacing="hint",
                                    hint_text="h"))
    reg.on_event_recorded_last()  # populated via on_event in practice
    assert reg.peek_most_recent_firing() is not None
    reg.drain_pending()
    # Peek still returns the last firing after the queue is drained.
    assert reg.peek_most_recent_firing() is not None
    assert reg.peek_most_recent_firing().pattern_id == "burnout"
```
- [ ] **Step 2: Run — expect FAIL** (`peek` returns None post-drain).
- [ ] **Step 3: Implement** — in `registry.py`: add `self._last_firing: PatternFiring | None = None` in `__init__`; in `on_event`, after `self._queue.append(firing)`, also set `self._last_firing = firing`; rewrite `peek_most_recent_firing` to `return self._last_firing`. (Remove the test's `on_event_recorded_last` stub — set `_last_firing` directly in the test instead.)
- [ ] **Step 4: Run the registry test file — expect PASS.**
- [ ] **Step 5: Commit** — `git add opencomputer/awareness/life_events/registry.py tests/test_life_events_registry.py && git commit -m "feat(life-events): decouple peek from the drained firing queue"`

---

## Task 2: `life_event_state.json` store

**Files:**
- Create: `opencomputer/awareness/life_events/state.py`
- Test: `tests/test_life_event_state.py`

Per-profile JSON tracking active teeth. Schema: `{pattern_id: {"firing_ts": float, "cron_id": str, "surfaced": bool, "verdict_pending": bool}}`.

- [ ] **Step 1: Write failing tests** — `load_state()` returns `{}` for a missing file; `save_state()` then `load_state()` round-trips; a corrupt file → `{}`.
- [ ] **Step 2: Run — expect FAIL** (module missing).
- [ ] **Step 3: Implement** — `state.py` with `load_state() -> dict` and `save_state(state: dict) -> None`, path `_home() / "life_event_state.json"`, atomic truncate-then-write, tolerate missing/corrupt (mirror `cli_awareness._load_muted`). Helpers: `mark_surfaced(pattern_id, cron_id)`, `clear(pattern_id)`, `verdict_pending_patterns() -> list[str]`.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit.**

---

## Task 3: `LifeEventInjectionProvider` — hint + tone

**Files:**
- Create: `opencomputer/awareness/life_events/injection.py`
- Test: `tests/test_life_event_injection.py`

A `DynamicInjectionProvider`: `provider_id = "life_event_hint"`, `priority = 60`, `async collect(ctx)`.

- [ ] **Step 1: Write failing tests** — a queued `"hint"` firing → `collect()` returns a `<life-event-hint>` block containing `hint_text` + the per-event tone directive; a muted pattern → skipped (`None`); empty queue → `None`.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** —
```python
_TONE_DIRECTIVES = {
    "burnout": "Respond gently and concisely; do not pile on tasks.",
    "exam_prep": "Keep replies focused and low-friction; the user is time-pressured.",
    "job_change": "Be encouraging and practical about the transition.",
    "travel": "Account for the user being away from their usual setup.",
}

class LifeEventInjectionProvider(DynamicInjectionProvider):
    priority = 60
    @property
    def provider_id(self) -> str: return "life_event_hint"
    async def collect(self, ctx: InjectionContext) -> str | None:
        reg = get_global_registry()
        firings = [f for f in reg.drain_pending() if not reg.is_muted(f.pattern_id)]
        if not firings:
            return None
        lines = []
        for f in firings:
            lines.append(f.hint_text)
            d = _TONE_DIRECTIVES.get(f.pattern_id)
            if d: lines.append(d)
        return "<life-event-hint>\n" + "\n".join(lines) + "\n</life-event-hint>"
```
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit.**

---

## Task 4: `life_event_actions` — cron follow-up lifecycle

**Files:**
- Create: `opencomputer/awareness/life_events/actions.py`
- Test: `tests/test_life_event_actions.py`

- [ ] **Step 1: Write failing tests** — `schedule_followup(firing)` creates a cron (assert via a `create_job` monkeypatch capturing kwargs) and records `cron_id` in state; a second `schedule_followup` for the same `pattern_id` while one is active is a no-op (dedup); `cancel_followup(pattern_id)` deletes the cron + clears state.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** —
  - `_FOLLOWUP_DELAY_DAYS = {"burnout": 3, "exam_prep": 7, "job_change": 5, "travel": 2}`.
  - `_CHECKIN_PROMPT = {...}` per pattern — a gentle, no-pressure check-in message.
  - `schedule_followup(firing, *, origin=None)`: if `state` already has a `cron_id` for this pattern → return (dedup). Else compute a one-shot `schedule` N days out, call `create_job(schedule=..., name=f"life-event check-in: {pattern_id}", prompt=_CHECKIN_PROMPT[pattern_id], notify="origin", origin_platform=..., origin_chat_id=...)`, record `cron_id` via `state.mark_surfaced`.
  - `cancel_followup(pattern_id)`: read `cron_id` from state, call the cron delete API, `state.clear(pattern_id)`.
  - **Investigation sub-step:** confirm `create_job`'s one-shot incantation (`repeat`/schedule form) and the cron-delete function name by reading `opencomputer/cron/jobs.py` — adjust the calls to the real signatures before finishing Step 3.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit.**

---

## Task 5: Create the cron at first surface

**Files:**
- Modify: `opencomputer/awareness/life_events/injection.py`
- Test: `tests/test_life_event_injection.py`

- [ ] **Step 1: Write failing test** — after `collect()` surfaces a firing, `life_event_state` has a `cron_id` for that pattern and `verdict_pending` is `True`.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — in `collect()`, for each surfaced firing not already in state: call `actions.schedule_followup(f, origin=<derived from ctx.runtime>)` and `state.mark_surfaced` with `verdict_pending=True`. Wrap in try/except — a cron failure must not break prompt assembly (fail-open, log WARNING).
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit.**

---

## Task 6: Post-turn classifier

**Files:**
- Create: `opencomputer/awareness/life_events/classifier.py`
- Test: `tests/test_life_event_classifier.py`

- [ ] **Step 1: Write failing tests** — `classify_response("I'm totally fine, not stressed", "burnout") == "refuted"`; `classify_response("yeah it's been rough", "burnout") in {"confirmed","unclear"}`; an empty / unrelated reply → `"unclear"`.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — `classify_response(user_text, pattern_id) -> Literal["refuted","confirmed","unclear"]`. v1 heuristic: a refutation-phrase set (`"i'm fine"`, `"i'm ok"`, `"not stressed"`, `"nothing's wrong"`, `"all good"`, `"doing well"`, `"you're wrong"`, …) — lowercased substring match → `"refuted"`; a light confirmation set → `"confirmed"`; else `"unclear"`. Conservative: only a clear refutation cancels. *(An LLM-backed classifier is a documented v2 enhancement — see spec §4.4; v1 ships heuristic-only: no extra inference, no provider plumbing.)*
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit.**

---

## Task 7: `STOP`-hook handler — run the classifier

**Files:**
- Modify: the hook-registration site (investigate — grep for an existing core `HookEvent.STOP` handler to copy the pattern)
- Create: the handler in `classifier.py`
- Test: `tests/test_life_event_classifier.py`

- [ ] **Step 1: Investigation** — grep `HookEvent.STOP` across `opencomputer/` to find how core registers a STOP handler; record the registration API.
- [ ] **Step 2: Write failing integration test** — state has a `verdict_pending` pattern with a `cron_id`; simulate a `STOP` hook with a refuting user message; assert `cancel_followup` ran (cron deleted, state cleared). Then a confirming message → cron kept, `verdict_pending` cleared.
- [ ] **Step 3: Run — expect FAIL.**
- [ ] **Step 4: Implement** — `on_stop_hook(ctx)`: for each `state.verdict_pending_patterns()`, classify the last user message; `"refuted"` → `actions.cancel_followup(pattern_id)`; else clear `verdict_pending`. Fail-open (a classifier error leaves the cron). Register it on the hook engine.
- [ ] **Step 5: Run — expect PASS. Commit.**

---

## Task 8: Register the provider + `oc awareness patterns status`

**Files:**
- Modify: provider registration site; `opencomputer/cli_awareness.py`
- Test: `tests/test_cli_awareness_patterns_status.py`

- [ ] **Step 1: Investigation** — grep for where `DynamicInjectionProvider`s are registered (e.g. plan-mode provider); record the API.
- [ ] **Step 2: Register** `LifeEventInjectionProvider` there.
- [ ] **Step 3: Write failing test** — `oc awareness patterns status` with a seeded `life_event_state.json` shows the active pattern + cron id + verdict-pending column; empty state → friendly empty line.
- [ ] **Step 4: Implement** `status` on `patterns_app` in `cli_awareness.py` — a Rich table from `load_state()`.
- [ ] **Step 5: Run — expect PASS. Commit.**

---

## Task 9: End-to-end integration test + docs

**Files:**
- Test: `tests/test_life_event_teeth_e2e.py`
- Create: `docs/awareness/life-events.md`

- [ ] **Step 1: Write the E2E test** — seed a Burnout firing into the registry queue → run `LifeEventInjectionProvider.collect()` (asserts hint injected + cron created + `verdict_pending`) → invoke the `STOP` handler with a refuting message (asserts cron cancelled) → repeat with a confirming message (asserts cron kept).
- [ ] **Step 2: Run — expect PASS.**
- [ ] **Step 3: Write `docs/awareness/life-events.md`** — the 4 hinted patterns, the hint/tone/cron behaviour, the self-correcting classifier, `oc awareness patterns {list,mute,unmute,status}`.
- [ ] **Step 4: Run the full `tests/test_life_event*.py` + `tests/test_cli_awareness*.py` bucket + `ruff check` — expect green.**
- [ ] **Step 5: Commit.**

---

## Self-review (done during planning)

- **Spec coverage:** §4.1 → Task 3; §4.2 → Task 4; §4.3 (cron at surface / STOP hook) → Tasks 5+7; §4.4 classifier → Task 6; §4.5 state → Task 2; §4.6 CLI → Task 8; §7 pre-tasks → Pre-flight + Task-4/7/8 investigation sub-steps; §8 testing → every task + Task 9. The `peek`/drain interaction (§7 pre-task 1) → Task 1.
- **Placeholder scan:** Tasks 4, 7, 8 contain explicit *investigation sub-steps* (confirm `create_job` one-shot form, the cron-delete fn, the STOP-handler registration API, the provider-registration API) rather than guessed code — these are genuine, bounded verifications, not placeholders. All other steps carry concrete code.
- **Type consistency:** `PatternFiring` fields (`pattern_id`, `hint_text`, `surfacing`, `confidence`, `evidence_count`, `timestamp`) used consistently; state schema keys (`firing_ts`, `cron_id`, `surfaced`, `verdict_pending`) consistent across Tasks 2/5/7/8; `classify_response` return literal (`refuted`/`confirmed`/`unclear`) consistent in Tasks 6/7.

## Out of scope

LLM-backed classifier (v1 is heuristic — noted in Task 6); persona-registry overhaul; v2 sub-projects B (contradiction detector) and C (embeddings).
