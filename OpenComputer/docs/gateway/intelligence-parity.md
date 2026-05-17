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

| Milestone | Status |
|---|---|
| **M1 — observability** (telemetry table, `ParityProbe`, dispatcher instrumentation, `oc gateway diagnose`, footer-on for fresh installs) | **Shipped** |
| **M2 — telemetry** (synthetic-load run modelling the real config) | **Shipped** — see below |
| **M3 — fix the mechanisms** (all ten — see table below) | **Shipped — 10 of 10** |
| **M4 — document remaining work** | **Shipped** — `deferred-parity-work.md` |

### M2 — what the telemetry showed

A synthetic load (200 turns, modelling a default config: no routing
rules, no `bindings`, `enabled_plugins="*"`, footer off) found that on a
**vanilla install the conditional mechanisms cannot fire** — #1, #2, #6
and #8 need routing / bindings / a plugin allowlist to be configured.
#3 fires on long replies; #7/#9 fire structurally. M3 then fixed **all
ten** — #3 (the one content-loss bug), #7 (the casual register, the one
that actually affects a default-config user), #5 + #10 (telemetry
honesty), and #1/#2/#6/#8 *prophylactically* so the gap never appears if
routing is adopted later.

### M3 — the fixes

| # | Mechanism | Fix shipped |
|---|---|---|
| 1 | `prompt_override` | `RoutingRule.merge_with_builder` — append the template prompt instead of replacing the builder. Default off. |
| 2 | `tool_allowlist` | `gateway.tool_filter: profile\|wildcard` — `wildcard` gives the gateway the CLI's full tool surface. Default `profile`. |
| 3 | `reply_truncation` | The outgoing drainer chunks over-cap bodies into ordered `(i/N)` messages instead of truncating. Nothing dropped. |
| 5 | `no_interactive_consent` | The gateway already has working interactive consent (buttons + text reply); M3 made the telemetry honest — #5 fires only on turns that actually paid a consent round-trip, not structurally. |
| 6 / 8 | `profile_rebind` / `routing_decision_invisible` | A one-line `↪ routed: …` badge on the first routed reply of a session. |
| 7 | `persona_casual_register` | `display.persona_override` — pin a persona id, or `none`/`off` to suppress the platform-driven casual register entirely. |
| 9 | `runtime_footer_off` | Footer on for fresh installs (M1). |
| 10 | `compaction_long_session` | Telemetry bug fixed (durable `compactions_count` delta). **And** `CompactionConfig.preserve_anchor` (default on) keeps the session's first user message verbatim across every compaction — a long session never re-summarises away its origin context. |

**All ten mechanisms are fixed.** The one item not built —
async-consent non-serialization — is a latency *enhancement*, not a
parity gap; see `deferred-parity-work.md`.

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
| `opencomputer/gateway/dispatch.py` | Per-turn instrumentation (all 10 mechanisms) + routing badge (#6/#8) |
| `opencomputer/gateway/reply_chunker.py` | `chunk_text` — split-don't-truncate (#3 fix) |
| `opencomputer/gateway/outgoing_drainer.py` | Chunk-and-send over-cap notification bodies |
| `opencomputer/gateway/agent_loop_factory.py` | `gateway.tool_filter` resolution (#2 fix) |
| `opencomputer/agent/routing.py` | `ResolvedTemplate.merge_with_builder` (#1 fix) |
| `opencomputer/cli_gateway.py` | `oc gateway diagnose` command |
