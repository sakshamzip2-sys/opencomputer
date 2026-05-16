# Life-event "teeth"

The agent watches the signal bus for **life events** — patterns in how you
work and talk that suggest something larger is going on: a job change, an
exam coming up, a burnout slide, a stretch of travel. Detecting a life event
used to be silent bookkeeping. **"Teeth"** means a detection now produces
*visible behaviour*: the next reply is gently tuned to the moment, and the
agent quietly schedules itself to circle back and check in a few days later.

This doc covers what fires teeth, what the behaviour looks like, how the
agent self-corrects when it guessed wrong, the CLI controls, and two honest
v1 limitations.

## What fires teeth — the six patterns

The life-events subsystem (`opencomputer/awareness/life_events/`) runs six
detector patterns. Each one declares a **`surfacing`** mode that decides
whether it grows teeth:

| Pattern | `surfacing` | Teeth? |
|---|---|---|
| `burnout` | `hint` | yes |
| `exam_prep` | `hint` | yes |
| `job_change` | `hint` | yes |
| `travel` | `hint` | yes |
| `health_event` | `silent` | **no** |
| `relationship_shift` | `silent` | **no** |

Only the four **`surfacing="hint"`** patterns produce teeth. The two
`silent` patterns — `health_event` and `relationship_shift` — are
deliberately teeth-free: they are sensitive enough that an unprompted
"noticed something" hint, or an unprompted check-in cron days later, would
be intrusive rather than caring. A silent firing is still recorded for the
awareness graph; it just never reaches the chat queue and never schedules a
cron.

## The behaviour

When a non-silent pattern fires, two things happen.

### 1. The next turn's prompt gets a `<life-event-hint>` block

`LifeEventInjectionProvider` (`injection.py`) is a per-turn injection
provider. At the start of the next turn it drains the pending firing and
contributes a one-line block to the prompt:

```
<life-event-hint>
your work rhythm looked like it shifted
Respond gently and concisely; do not pile on tasks.
</life-event-hint>
```

The first line is the detector's own `hint_text`. The second is a
**per-event tone directive** — a short instruction that nudges the reply's
register without scripting it:

| Pattern | Tone directive |
|---|---|
| `burnout` | Respond gently and concisely; do not pile on tasks. |
| `exam_prep` | Keep replies focused and low-friction; the user is time-pressured. |
| `job_change` | Be encouraging and practical about the transition. |
| `travel` | Account for the user being away from their usual setup. |

The block is *injected* rather than spliced into the system prompt: the base
system prompt is frozen per session for prefix-cache hits, and the injection
engine is the canonical surface for per-turn cross-cutting context.

### 2. A gentle proactive check-in cron is scheduled

Surfacing a hint also schedules a **one-shot "gentle check-in" cron** N days
out (`actions.schedule_followup`). The agent circles back on its own to ask
how you are doing — once, without nagging:

| Pattern | Check-in fires after |
|---|---|
| `burnout` | 3 days |
| `exam_prep` | 7 days |
| `job_change` | 5 days |
| `travel` | 2 days |

The delays are tuned per pattern — travel resolves fast (you are back from
the trip), exam prep is the longest arc (wait until the exam window has
passed). The cron is one-shot: it fires exactly once, then it is gone.

Scheduling is **dedup-guarded** and **fail-open**. A hint that re-fires
while its follow-up is still active never schedules a second cron; and a
cron-backend failure is logged at WARNING but never blocks prompt assembly —
the `<life-event-hint>` block still surfaces.

## The self-correcting classifier

The agent's life-event inference is a guess, and a guess can be wrong. So
after surfacing a hint the agent **listens to your reply** and self-corrects.

A post-turn **`STOP` hook** (`classifier.on_stop_hook`) runs at the end of
every turn. For each pattern with a hint awaiting a verdict it judges your
most recent reply:

- A **refuting** reply ("I'm totally fine, not stressed") → the follow-up
  cron is **cancelled** and the whole tooth is dropped. You said you are
  fine; the agent will not circle back.
- A **confirming** reply ("yeah I'm really burnt out") → the cron is
  **kept**. The gentle check-in still fires days later.
- An **unclear** reply — ambiguous, off-topic, empty → the cron is **kept**.
  When in doubt the agent does not cancel.

### Turn-index timing

A hint surfaces on turn N. The agent's turn-N reply acknowledges the life
event; *you* respond to that on turn **N+1**. But the `STOP` hook fires at
the end of every turn — including turn N's own. So the classifier records
the **surfacing turn** and only judges a reply on a turn *strictly later*
than it. Turn N's own `STOP` is skipped — the turn-N message you typed
predates the hint-influenced reply, and a coincidental "I'm fine" in it must
not wrongly cancel the check-in.

### It is a conservative v1 heuristic

The v1 classifier is **substring matching** on a lowercased reply — phrase
sets for refutations, confirmations, and direct rebuttals, with a negation
guard so "i'm **not** doing well" is not mistaken for the refutation phrase
"doing well". It is deliberately **conservative**: only a *clear* refutation
cancels, because the failure costs are asymmetric — a false "refuted"
cancels a check-in you wanted (bad), while a missed refutation merely leaves
a gentle one-shot cron that fires once and is gone (mild).

A substring heuristic cannot truly *infer* sentiment. **The v2 upgrade path
is a documented LLM-backed classifier** — `classify_response` already takes
the `pattern_id` so a v2 implementation can branch on it. v2 is explicitly
out of scope for v1.

The hook is **fail-open**: a classifier or state error is logged at WARNING
and leaves the cron untouched — an error must never mis-cancel a wanted
check-in.

## CLI — `oc awareness patterns`

The four life-event patterns are inspectable and controllable from the CLI:

| Command | Purpose |
|---|---|
| `oc awareness patterns list` | List every pattern + its `surfacing` mode + whether it is muted. |
| `oc awareness patterns status` | Show the **active teeth** — one row per pattern with a pending or scheduled follow-up cron, from `life_event_state.json`. |
| `oc awareness patterns mute <id>` | Mute a pattern — **no hint, no cron**. Persisted across sessions. |
| `oc awareness patterns unmute <id>` | Unmute a previously-muted pattern. |

`status` is the window into the live state: it reads
`life_event_state.json` and prints, per pattern, the follow-up `cron_id` and
whether the reply is still verdict-pending. An empty state prints a friendly
one-liner — no table, no crash.

`mute` is the off switch. A muted pattern surfaces no `<life-event-hint>`
block and schedules no check-in cron — the detector still runs, it just
grows no teeth. Mute state is persisted at
`$OPENCOMPUTER_HOME/awareness/muted_patterns.json` and re-applied at the
start of every session.

## Honest v1 limitations

Three parts of the feature are intentionally not finished in v1. They are
called out here so the feature is not assumed to be more complete than it
is.

### 1. The check-in cron is not channel-targeted

The injection layer cannot see which channel you are talking on.
`RuntimeContext` carries only mode flags (plan / yolo / …), not a channel
identity, and `InjectionContext` does not surface the `RequestContext` that
*does* carry `platform` / `chat_id` / `thread_id`. So
`LifeEventInjectionProvider.collect` schedules the follow-up cron with
**`origin=None`** — the cron is created and it will fire, but it is **not
routed back to a specific chat**. (`schedule_followup` *can* accept an
`origin` and thread it through to `notify="origin"`; the injection caller
simply has nothing to pass.) **v2** would thread the active channel through
to the injection layer so the check-in lands back in the conversation that
triggered it.

### 2. Hint injection is CLI-scoped

`LifeEventInjectionProvider` is registered in `_run_chat_session` —
alongside the other built-in injection providers like `ThinkingInjector` —
which is the **`oc chat` CLI path**. The `STOP`-hook classifier runs on
every surface (it registers against the singleton hook engine in
`AgentLoop.__init__`), but with **no hint injected outside the CLI** the
feature is effectively **CLI-only** for now: a life event detected during a
gateway / Telegram / Discord / web-UI conversation grows no visible tooth.
**v2** would extend injection-provider registration to the gateway and
channel surfaces so teeth appear everywhere the agent runs.

### 3. A re-fired hint is re-shown but not re-judged while a cron is active

After a `confirmed` or `unclear` verdict, `clear_verdict_pending` leaves the
pattern's state entry in place with `verdict_pending=False` and the
follow-up `cron_id` still set. If that same pattern fires *again* later,
`LifeEventInjectionProvider.collect` surfaces a fresh `<life-event-hint>`
block — but `schedule_followup` sees the existing `cron_id`, takes its
dedup branch, and returns early *without* calling `mark_surfaced`. So
`verdict_pending` stays `False`: the re-fired hint **is shown to the user
but the `STOP` classifier never re-judges the reply to it** until the
existing check-in cron resolves and the entry is cleared. The first verdict
for a pattern sticks for the lifetime of that pattern's active cron. **v2**
would re-arm `verdict_pending` when a hint re-surfaces for a pattern whose
follow-up is still pending, so each re-shown hint gets its own verdict.

## Where the code lives

```
opencomputer/awareness/life_events/
├── pattern.py        LifeEventPattern base + PatternFiring
├── burnout.py  exam_prep.py  job_change.py  travel.py        ← hint patterns
├── health_event.py  relationship_shift.py                    ← silent patterns
├── registry.py       LifeEventRegistry — bus subscription + firing queue
├── injection.py      LifeEventInjectionProvider — the <life-event-hint> block
├── actions.py        schedule_followup / cancel_followup — the check-in cron
├── classifier.py     classify_response + on_stop_hook — the self-correction
└── state.py          life_event_state.json — the active-teeth store
```

The end-to-end flow (firing → hint + cron → reply → self-correction) is
covered by `tests/test_life_event_teeth_e2e.py`.
