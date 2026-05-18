# Dynamic Thinking Budget — Design (presentation only, not yet built)

Date: 2026-05-18
Owner: Saksham
Status: **Design presented for review.** Not implemented. The static
default bump (subagent→medium, Sonnet→high, Opus→max, OpenAI→high)
shipped separately in the same change; this doc covers the *dynamic*
system that would supersede a purely static policy.

This document follows the senior-engineer flow: brainstorm →
audit-design → plan. It is a **presentation** — execution is gated on a
separate go-ahead.

---

## Problem

`reasoning_effort` (`minimal → low → medium → high → xhigh → max`) is
the "thinking budget". Today `effort_policy.recommended_effort` is a
**static** policy: a per-model default, overridable by `/reasoning`.

A static budget cannot feel smart:

- Fixed `high` makes *every* turn slow — including "hi" and "thanks".
- Fixed `low` underthinks the hard turns.

A human expert does not deliberate before saying "you're welcome" and
does think hard before refactoring a module. "Smart" = *effort matched
to difficulty*. That is dynamic behaviour. It is also inherently
cost-optimising — cheap turns stay cheap — which matters most for
always-on connector bots that the CLI's interactive model never had.

---

## Phase 1 — Brainstorm (8 approaches)

| # | Approach | Effort | Risk | Upside |
|---|---|---|---|---|
| 1 | Keyword/heuristic classifier (scan prompt for cues) | S | Low | Cheap, transparent |
| 2 | LLM pre-classification call ("rate this 1-5") | M | Med | Accurate — but an extra call + latency on *every* turn |
| 3 | Length/structure heuristic (tokens, code blocks, `?` count) | S | Low | Cheap, but crude alone |
| 4 | Outcome-feedback escalation (retry/failure → think harder next) | S | Low | Self-correcting |
| 5 | Tool-depth signal (many tool calls last turn → harder task) | S | Low | Good lagging signal |
| 6 | **Hybrid signal blend** (1+3+4+5 → score → adjust) | M | Low | Robust; no extra call |
| 7 | Explicit user cues ("think hard" / "quick") | XS | Low | Direct intent, free |
| 8 | Two-pass ramp: think low, if first block looks uncertain re-run higher | M | High | Accurate — but doubles cost/latency |

**Converge:** #6 (hybrid blend) as the engine, + #7 (user cues, free
intent signal) + #4 (outcome feedback) folded into the blend. Reject #2
and #8 on merit — both add a call/latency tax to every turn, defeating
the cost win. #1/#3/#5 alone are too crude; blended they are robust.

**Chosen: a no-extra-call hybrid scorer, applied asymmetrically.**

---

## Phase 2 — Audit-design (9 lenses)

| Lens | Finding | Resolution |
|---|---|---|
| Assumption | "Difficulty is detectable from the prompt" — only partly true | **Asymmetric bias**: eager to scale *up*, reluctant *down*. When unsure, think more. Quality is never gambled; cost savings come only from clearly-trivial turns. |
| Architecture stress | Edge cases: empty prompt, attachment-only turn, `collect`-merged burst | Scorer takes the *final* user text (post-merge); attachment-only → treat as default (not trivial). |
| Alternative dismissal | LLM-classifier (#2) rejected on cost/latency merit, not default | Documented above. |
| Requirement gap | User must *see* the chosen level and be able to *override* and *bound* it | M3 (transparency) + M4 (control) are first-class, not afterthoughts. |
| Composability | Must fit `effort_policy` (already returns a level), `runtime_state` (per-chat pin — A2 pattern), `Binding` (per-connector — A6 pattern) | The dynamic layer *adjusts* `recommended_effort`'s output; pin/bounds are separate inputs. Clean seams already exist. |
| Scope honesty | The scorer heuristic is the real work; plumbing is cheap | M1 is the hard milestone; M3/M4 reuse shipped patterns. |
| API stability | `reasoning_effort` ladder is stable; dynamic layer is additive | No breaking change — `/reasoning <level>` still pins; `/reasoning auto` is new. |
| Failure map | Heuristic underthinks → bad answer (mitigated by asymmetry) · overthinks → cost (mitigated by ceiling) · scorer raises → must fall back to static default | Scorer wrapped: any error → static `recommended_effort`. |
| YAGNI | Depleting daily-token budget — no caller needs it *yet* | Ship the **ceiling hook**; defer the metering/reset-window machinery until there is real demand. |

All findings resolved or accepted. Design holds.

---

## Phase 3 — Plan

**Done =** the thinking budget auto-scales per turn, the user can see
the level used, pin it, and bound it per chat / per connector — with
the static policy as the safe fallback.

| Milestone | Tasks | Size | Notes |
|---|---|---|---|
| **M1 — scorer** (MVP) | `effort_signal.py`: pure function `score_turn(text, runtime, last_outcome) → delta`. Signals: length/structure, complexity keywords, explicit user cues, retry-after-failure, prior tool depth. TDD. | M | The real engineering. Pure + fully testable. |
| **M2 — dynamic layer** | `effort_policy`: `recommended_effort` gains a dynamic mode — apply `score_turn` delta to the static default, asymmetric clamp. `/reasoning auto` enables it; explicit `/reasoning <level>` still pins. | S | Additive; static path unchanged when `auto` off. |
| **M3 — transparency** | Show the level used — footer / `/reasoning status` / a one-line indicator. Gateway: reuse the A7 footer surface. | S | "Show the user the thinking level." |
| **M4 — control** | `/reasoning auto` · per-chat pin persisted in `runtime_state.json` (A2 pattern) · per-connector floor/ceiling via `Binding.reasoning_floor`/`reasoning_ceiling` (A6 pattern). | S | Reuses two shipped patterns. |
| **M5 — depleting budget** (deferred) | Daily per-connector thinking-token budget that tightens the ceiling as it depletes; resets on a window. Sits on top of M4's ceiling hook. | M | Separate spec; only when demand is real. |

**MVP = M1 + M2.** That alone delivers auto-scaling. M3/M4 make it
transparent and controllable; M5 is the cost-cap, deferred.

---

## Phase 4 — Audit-plan (harsh critic)

- *Undersized?* M1 is honestly M, not S — the scorer's signal weighting
  needs tuning against real transcripts. Flagged.
- *What breaks if M1 slips?* Nothing ships — M2-M4 all depend on the
  scorer. M1 is the critical path; that is correct sequencing.
- *Simpler path?* Yes, partially: ship `/reasoning auto` as a thin
  length-only heuristic first, refine signals later. Acceptable de-risk
  if M1 proves slow.
- *Retro regret risk:* tuning the scorer by guesswork. Mitigation —
  drive weights from a labelled set of real prompts, not intuition.

Plan holds, with M1 honestly sized M.

---

## Recommendation

Build **M1 + M2** as the MVP behind `/reasoning auto` (default off
initially → zero behaviour change, opt-in proves it), then M3/M4, then
default `auto` on once the scorer is trusted. M5 stays deferred.

This makes OC genuinely feel smarter (depth matched to difficulty),
costs less than a static-high policy, keeps the user informed and in
control, and absorbs the per-connector depleting-budget idea as M5's
ceiling input — one coherent system, not two features.
