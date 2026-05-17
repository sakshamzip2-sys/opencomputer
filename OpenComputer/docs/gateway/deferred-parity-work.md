# Gateway-vs-CLI parity — deferred work

Milestone 4 of `docs/superpowers/specs/2026-05-17-gateway-vs-cli-parity/PLAN.md`.

M3 fixed six of the ten parity mechanisms (#1, #2, #3, #6, #8, plus #9
in M1 and the #10 *telemetry* bug). This file is the honest record of
what was **not** fixed and why — so a future session has a real map,
not a vague "TODO: improve gateway."

See `intelligence-parity.md` for the shipped work and `oc gateway
diagnose --rollup` for live telemetry.

---

## #5 — `no_interactive_consent`

**What it is.** A CLI session prompts for tool approval synchronously
(`y/n` in the terminal) and the agent gets the answer mid-turn. A
gateway session cannot — it posts an approval message and must wait for
a button click or text reply. Multi-tool turns serialise badly, and the
agent learns to avoid gated tools on the gateway because the round-trip
is expensive.

**Why deferred.** The fix is *async consent* — let the turn continue
past a pending approval and resume when the click lands, rather than
blocking the whole turn. That is a genuine multi-week subsystem change:
it touches the consent gate's state machine, the dispatcher's per-chat
lock, turn resumption, and every adapter's button surface. It is its own
spec, not a fix that fits this milestone.

**Severity in practice.** MEDIUM, and only when the agent actually wants
a gated tool (Bash, Edit, …). On a chat-only gateway profile it rarely
bites. The structural telemetry row (`no_interactive_consent`, fires
every turn) records the *capability gap*, not a per-turn failure.

**Fix sketch.** A new spec: `consent/async-approval-design.md` — pause
the tool-dispatch loop at a gated call, persist a pending-approval
record, return control, and re-enter the loop on the approval event.

---

## #7 — `persona_casual_register`

**What it is.** The persona classifier sees `platform="telegram"` (or
any chat channel) and leans toward a casual register — shorter, less
planning-heavy replies. This is the single mechanism that *does* fire on
a default config and *does* change how the agent feels.

**Why deferred.** It is **deliberate behaviour** — the agent is supposed
to match the register of the surface it is on. "Fixing" it is a product
decision, not a bug fix: do you *want* your Telegram agent to answer
like a CLI coding session? For some users yes, for most no. Shipping a
behaviour flip inside this milestone would be wrong.

**The lever (not yet wired).** The intended fix is a
`display.persona_override` config key — set it to `task` to force the
task-oriented register on gateway sessions regardless of platform. It is
small (the persona resolver already takes a mode; this just pins it),
but it is opt-in product surface and belongs in its own change with its
own pre-mortem.

**If you want it now:** this is the most likely real improvement for a
default-config user who feels the gateway is "dumber." Ask for
`display.persona_override` to be implemented — it is an S-sized task.

---

## #10 — `compaction_long_session` (the context loss, not the telemetry)

**What it is.** Long-lived gateway sessions (months of occasional
messages) accumulate enough history that `CompactionEngine` summarises
away the early turns — including preferences and project context the
user set up long ago. A fresh CLI session never hits this.

**What M3 *did* fix.** Only the **telemetry**: mechanism #10 was
mis-detecting via the shared `DEFAULT_RUNTIME_CONTEXT` and over-reporting
~20× (see `intelligence-parity.md`). It now uses a durable
`compactions_count` delta.

**Why the underlying issue is deferred.** Actually preventing the
context loss needs *session-fork-aware compaction* — recognising that a
long gateway session should preserve a durable "about this user / this
project" core across compactions rather than summarising it away. That
is an XL change to the compaction engine and its own spec
(`compaction/durable-core-design.md`), not a milestone fix.

**Severity in practice.** MEDIUM, slow-burn — only sessions older than a
few weeks with many turns.

---

## Mechanisms with no deferred work

#1, #2, #3, #6, #8 — fixed in M3. #9 — fixed in M1. Their entries in
`intelligence-parity.md` reflect the shipped state.
