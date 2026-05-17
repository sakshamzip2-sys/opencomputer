# Gateway-vs-CLI parity — deferred work

Milestone 4 of `docs/superpowers/specs/2026-05-17-gateway-vs-cli-parity/PLAN.md`.

M3 fixed **nine of the ten** parity mechanisms (#1, #2, #3, #5, #6, #7,
#8, plus #9 in M1 and the #10 *telemetry* bug). This file is the honest
record of the one piece of genuinely deferred work and the one optional
enhancement — so a future session has a real map.

See `intelligence-parity.md` for the shipped work and `oc gateway
diagnose --rollup` for live telemetry.

---

## Genuinely deferred — #10 compaction context-loss

**What it is.** Long-lived gateway sessions (months of occasional
messages) accumulate enough history that `CompactionEngine` summarises
away the early turns — including preferences and project context set up
long ago. A fresh CLI session never hits this.

**What M3 *did* fix.** Only the **telemetry**: mechanism #10 was
mis-detecting via the shared `DEFAULT_RUNTIME_CONTEXT` and over-reporting
~20× (synthetic load: 97% vs the real ~4%). It now uses a durable
`compactions_count` before/after delta and is accurate.

**Why the underlying issue is deferred.** Actually preventing the
context loss needs *session-fork-aware compaction* — recognising that a
long gateway session should preserve a durable "about this user / this
project" core across compactions rather than summarising it away. That
is an XL change to the `CompactionEngine` and deserves its own spec
(`compaction/durable-core-design.md`), not a milestone fix.

**Severity in practice.** MEDIUM, slow-burn — only sessions older than a
few weeks with many turns. The accurate #10 telemetry will show, over
time, how often it actually bites; revisit if the rollup says it is
frequent.

---

## Optional enhancement — #5 async-consent non-serialization

**Not a gap.** The gateway already has working interactive consent —
inline approval buttons (`_send_approval_prompt` / `_handle_approval_click`)
and text "yes/no" replies (`_maybe_resolve_consent_text_reply`). M3 made
mechanism #5's telemetry honest: it now fires only on turns that
actually paid a consent round-trip, not structurally on every turn.

**The enhancement.** While the ConsentGate blocks for a button click,
the per-chat lock is held — a multi-tool turn serialises. A future
optimisation could let the turn continue past a pending approval and
resume on the click. That is a `consent/async-approval-design.md` spec,
not a parity gap: nothing is broken, it is purely a latency improvement
for the rare multi-gated-tool turn.

---

## Fixed — no deferred work

| # | Mechanism | Fix |
|---|---|---|
| 1 | `prompt_override` | `RoutingRule.merge_with_builder` |
| 2 | `tool_allowlist` | `gateway.tool_filter: wildcard` |
| 3 | `reply_truncation` | drainer chunk-and-send (`reply_chunker`) |
| 5 | `no_interactive_consent` | telemetry made honest; consent already works |
| 6 / 8 | `profile_rebind` / `routing_decision_invisible` | `↪ routed:` badge |
| 7 | `persona_casual_register` | `display.persona_override` (pin or `none`) |
| 9 | `runtime_footer_off` | footer on for fresh installs (M1) |
| 10 | `compaction_long_session` | telemetry fixed (context-loss deferred, above) |
