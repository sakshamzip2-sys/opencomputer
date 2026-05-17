# Gateway-vs-CLI intelligence parity

Why an OpenComputer session on Telegram / Discord / Slack / any of the
18 channel adapters can *feel* less capable than the same agent on the
CLI — and how to see exactly which mechanisms are responsible on **your**
install.

- **Diagnosis (background reading):** `docs/superpowers/specs/2026-05-17-gateway-vs-cli-intelligence-gap/ANALYSIS.md`
- **Plan:** `docs/superpowers/specs/2026-05-17-gateway-vs-cli-parity/PLAN.md`
- **This doc** covers Milestone 1 — the observability surface that shipped.

---

## TL;DR

The CLI and the gateway construct the *same* `AgentLoop`. There is no
"lite" agent. But **ten mechanisms** quietly make a gateway turn behave
differently from a CLI turn. Milestone 1 instruments all ten so you can
*measure* which ones fire on your traffic, instead of guessing.

```bash
oc gateway diagnose            # per-turn: what fired on recent turns
oc gateway diagnose --rollup   # aggregate: fire-rate + priority per mechanism
```

---

## The ten mechanisms

| # | id | What it does | Severity |
|---|---|---|---|
| 1 | `prompt_override` | A routing rule's system prompt **replaces** the whole PromptBuilder — declarative / skills / memory / SOUL injection all switch off. | CRITICAL (4) |
| 2 | `tool_allowlist` | The gateway loop was built with a non-wildcard `allowed_tools` set; the CLI never restricts tools. | HIGH (3) |
| 3 | `reply_truncation` | The reply exceeded the platform message cap and was cut by `truncate_smart`. | HIGH (3) |
| 4 | `channel_prompt_overlay` | `_build_channel_runtime` injected a channel-scoped prompt and/or skill bodies. | HIGH (3) |
| 5 | `no_interactive_consent` | Gateway turns cannot prompt for tool approval synchronously — consent is an async button/text round-trip. | MEDIUM (2) |
| 6 | `profile_rebind` | A bindings/routing rule rebound the turn to a different profile (its own MEMORY/USER/SOUL + model). | HIGH (3) |
| 7 | `persona_casual_register` | The turn carries a chat `agent_context`; the persona overlay leans casual vs. a CLI task session. | MEDIUM (2) |
| 8 | `routing_decision_invisible` | A routing/binding rule changed behaviour, but the user saw no chat-visible badge explaining it. | MEDIUM (2) |
| 9 | `runtime_footer_off` | `display.runtime_footer.enabled` is false, so the reply shows no `model · context% · cwd` line. | MEDIUM (2) |
| 10 | `compaction_long_session` | `CompactionEngine` summarised earlier history this turn; long gateway sessions lose context CLI sessions still hold. | MEDIUM (2) |

The catalogue is defined once, in
`opencomputer/gateway/parity_probe.py::MECHANISMS` — both the dispatcher
instrumentation and `oc gateway diagnose` import it.

---

## How the telemetry works

Every gateway turn, `Dispatch.__do_dispatch_inner` builds one
`ParityProbe`, records which mechanisms fired as it evaluates them, and
flushes **ten rows** (one per mechanism) into the
`gateway_parity_log` table of the profile's `audit.db` (schema v21).
A mechanism that was evaluated but did not fire is written `fired=0`,
so the rollup denominator is always a clean per-mechanism turn-count.

- Writes are **best-effort** — a SQLite failure is logged at WARNING and
  swallowed (the three-tier-swallow contract: telemetry never wedges the
  agent loop).
- The outgoing drainer additionally records a `reply_truncation` row
  (with sentinel `turn_id = 0`) when it truncates a notification body.
- Cost: one `executemany` of ten rows per turn, ~1 ms. Off the critical
  path — it flushes in the dispatch `finally` block.

### Inspecting it

```bash
# Per-turn view — the last 20 turns, newest first.
oc gateway diagnose

# Filter to one session.
oc gateway diagnose --session <session-id>

# Aggregate — fire-rate and priority per mechanism. The top-3 (marked →)
# are the candidates Milestone 3 fixes first. Priority = fire-rate × severity.
oc gateway diagnose --rollup --since 7d

# Machine-readable.
oc gateway diagnose --rollup --json
```

`--since` accepts `7d` / `12h` / `90m` / `3600` (a bare number is seconds).

---

## What shipped vs. what's deferred

This is **Milestone 1 of 4**. M1 ships *observability only* — it does
not change agent behaviour.

| Milestone | Status |
|---|---|
| **M1 — observability** (telemetry table, `ParityProbe`, dispatcher + drainer instrumentation, `oc gateway diagnose`, footer-on for fresh installs) | **Shipped** |
| M2 — telemetry collection window (≥1 week of real gateway traffic, then pick the top-3 mechanisms by `priority_score`) | Pending real traffic |
| M3 — fix the top-3 mechanisms identified by M2 | Gated on M2 |
| M4 — document the remaining mechanisms as deferred | Gated on M3 |

Until M2 has telemetry, **all ten mechanisms are deferred** — M3 must
not pick its top-3 from intuition (that is the Approach-D trap the plan
explicitly rejects). Run `oc gateway diagnose --rollup` after a week of
use; the head of that list is the M3 scope.

If a profile has very low gateway volume (<50 turns/week), the rollup
will be statistically thin — see PLAN.md §4.4 for the synthetic-load
fallback.

---

## runtime_footer for fresh installs (T1.8)

Mechanism #9 is the simplest to close: turn the footer on. As of M1, the
three bundled config variants (`oc config init --variant lax|strict|sandbox`)
ship `display.runtime_footer.enabled: true`, so **fresh installs** see a
`model · context% · ~/cwd` line on every gateway reply.

**Existing installs are not touched.** A `config.yaml` with no `display:`
section keeps the historical OFF default — important for bot deployments
that scan reply text for keywords. To opt in on an existing install:

```bash
oc config set display.runtime_footer.enabled true
```

> **Wiring note.** M1 also fixed a latent bug: the top-level `display:`
> config section was silently dropped by `load_config` (there was no
> `Config.display` field) and never reached the gateway dispatcher — the
> footer knob was effectively dead. `Config` now carries `display` as a
> dict and `Gateway` forwards it into `Dispatch`.

---

## Files

| File | Role |
|---|---|
| `opencomputer/gateway/parity_probe.py` | Mechanism catalogue, `ParityProbe`, writers + readers |
| `opencomputer/agent/state.py` | `gateway_parity_log` table — schema v21 migration |
| `opencomputer/gateway/dispatch.py` | Per-turn instrumentation (all 10 mechanisms) |
| `opencomputer/gateway/outgoing_drainer.py` | Notification-path truncation telemetry |
| `opencomputer/cli_gateway.py` | `oc gateway diagnose` command |
