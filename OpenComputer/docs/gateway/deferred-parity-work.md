# Gateway-vs-CLI parity — remaining work

Milestone 4 of `docs/superpowers/specs/2026-05-17-gateway-vs-cli-parity/PLAN.md`.

**All ten parity mechanisms are fixed.** This file records the one
*optional enhancement* that was deliberately not built — because it is a
latency optimisation, not a parity gap — so a future session has an
honest map.

See `intelligence-parity.md` for the shipped work and `oc gateway
diagnose --rollup` for live telemetry.

---

## Optional enhancement — #5 async-consent non-serialization

**Not a gap — this is an optimisation.** The gateway already has working
interactive consent: inline approval buttons (`_send_approval_prompt` /
`_handle_approval_click`) and text "yes/no" replies
(`_maybe_resolve_consent_text_reply`). M3 made mechanism #5's telemetry
honest — it now fires only on turns that actually paid a consent
round-trip, not structurally on every turn.

**The enhancement.** While the `ConsentGate` blocks for a button click,
the per-chat lock is held — so a turn that needs *several* gated tools
serialises each approval. A future optimisation could let the turn
continue past a pending approval and resume on the click. That belongs
in its own spec (`consent/async-approval-design.md`): nothing is broken,
it is purely a latency improvement for the rare multi-gated-tool turn.

It was not built because (a) it is an enhancement, not a parity gap, and
(b) it touches the consent state machine, the per-chat lock and turn
resumption — a real subsystem change that should not be rushed.

---

## All ten mechanisms — fixed

| # | Mechanism | Fix |
|---|---|---|
| 1 | `prompt_override` | `RoutingRule.merge_with_builder` — append, don't replace the builder |
| 2 | `tool_allowlist` | `gateway.tool_filter: wildcard` — full CLI tool surface |
| 3 | `reply_truncation` | drainer chunk-and-send (`reply_chunker`) — nothing dropped |
| 5 | `no_interactive_consent` | telemetry made honest; consent already works (buttons + text) |
| 6 / 8 | `profile_rebind` / `routing_decision_invisible` | `↪ routed:` badge on the first routed reply |
| 7 | `persona_casual_register` | `display.persona_override` — pin a persona or `none` to suppress |
| 9 | `runtime_footer_off` | footer on for fresh installs |
| 10 | `compaction_long_session` | telemetry fixed (durable `compactions_count` delta) **and** `CompactionConfig.preserve_anchor` keeps the session's first user message verbatim across every compaction, so a long session never loses its origin context |
