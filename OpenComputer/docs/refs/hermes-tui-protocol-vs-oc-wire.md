# Hermes TUI protocol vs. OC wire — Milestone 1 mapping spike

Date: 2026-05-17
Spec: `docs/superpowers/specs/2026-05-17-tui-parity/TUI.md`
Milestone: **M1 — Protocol mapping spike (the MVP GO/NO-GO gate)**
Author: Saksham
Branch: `feat/oc-tui-parity-2026-05-17`

> **The gate, in one line:** the spec (TUI.md §Phase 3 M1, §4.1) says *"any
> gap >30% triggers a stop-and-escalate; if M1 fails, the plan reverts to
> Approach C."* This spike measures that gap.
>
> **Result: ~87% of Hermes' TUI RPC surface is `missing` from OC's wire.
> The gate FAILS. Verdict: NO-GO on Approach H. See §5.**

---

## 1. Method — how this was measured (verified, not guessed)

### Hermes TUI RPC + event surface

- Source read: `~/.hermes/hermes-agent/ui-tui/src/gatewayClient.ts` (700 LOC).
  It is a *generic* JSON-RPC transport — `request(method, params)` — so the
  method names live in the app layer, not the client.
- Method names extracted by grepping every dotted string literal across
  `ui-tui/src/**/*.ts{,x}` and cross-checking against the response-type
  interfaces in `gatewayTypes.ts` (each `FooResponse` interface = one RPC).
- Event names extracted from the `GatewayEvent` discriminated union in
  `gatewayTypes.ts:459-524`.

### OC wire RPC + event surface

- Source read: `opencomputer/gateway/wire_server.py::WireServer._dispatch`
  (the authoritative `if/elif` method router) and
  `opencomputer/gateway/protocol.py` (`METHOD_*` / `EVENT_*` constants) and
  `protocol_v2.py` (`METHOD_SCHEMAS`, `EVENT_SCHEMAS`).
- The hello handshake at `wire_server.py:337-361` advertises the canonical
  list — **11 methods, 8 events**.

---

## 2. The two surfaces, side by side

| | Hermes TUI needs | OC wire serves |
|---|---|---|
| RPC methods | **~54** | **11** |
| Server→client events | **~33** | **11** (8 advertised + 3 in `EVENT_SCHEMAS`) |

### OC's complete wire surface (the entire set)

RPC: `hello`, `chat`, `sessions.list`, `search`, `skills.list`,
`steer.submit`, `slash.list`, `slash.dispatch`, `permission.response`,
`memory.status`, `evolution.status`.

Events: `turn.begin`, `turn.end`, `tool.call`, `tool.result`,
`assistant.message`, `error`, `permission.request`, `memory.write`,
`evolution.tuning_changed`, `stream.retry`, `profile.swap`.

That is the whole thing. Anything below not in those two lists is `missing`.

---

## 3. RPC method mapping (Hermes → OC)

Classification per the spec's T1.3 rubric:

- **`direct`** — OC serves the same method, same shape, zero translation.
- **`adapter`** — OC has a wire backend that an adapter can reshape into
  Hermes' contract (rename, field remap, response restructure).
- **`missing`** — OC has **no wire backend**. Functionality may exist as a
  REST route or CLI command, but Hermes' `gatewayClient.ts` is
  WebSocket-JSON-RPC-only; serving it means writing a *new wire RPC handler*
  plus wiring its backend. That is net-new feature work, not "adapter" work.

| # | Hermes RPC | OC wire backend | Class | Note |
|---|---|---|---|---|
| 1 | `prompt.submit` | `chat` | adapter | OC `chat` is request/response-sync; Hermes `prompt.submit` acks then streams. Semantics differ. |
| 2 | `session.list` | `sessions.list` | adapter | Rename + row-shape remap. |
| 3 | `session.steer` | `steer.submit` | adapter | Rename; param `text`→`prompt`. |
| 4 | `slash.exec` | `slash.dispatch` | adapter | Rename; response `output`/`warning` vs `output`/`side_effects`. |
| 5 | `command.dispatch` | `slash.dispatch` | adapter | Collapses 4 `CommandDispatchResponse` variants onto one method. |
| 6 | `commands.catalog` | `slash.list` | adapter | Shape gap: catalog has `categories`/`canon`/`sub`/`skill_count`; OC returns a flat `commands` list. |
| 7 | `approval.respond` | `permission.response` | adapter | Only the *approval* leg of a 4-way consent surface (see #8-10). |
| 8 | `clarify.respond` | — | **missing** | OC has no clarify-request RPC pair. |
| 9 | `sudo.respond` | — | **missing** | No wire backend. |
| 10 | `secret.respond` | — | **missing** | No wire backend. |
| 11 | `session.create` | — | **missing** | Implicit in OC `chat(session_id=None)`; no explicit RPC returning a `SessionCreateResponse`. |
| 12 | `session.resume` | — | **missing** | Critical: returns the full transcript (`messages[]`). OC has no resume RPC; the TUI fakes it via `sessions.list`. |
| 13 | `session.delete` | — | **missing** | CLI-only (`oc session delete`). |
| 14 | `session.interrupt` | — | **missing** | No wire backend. |
| 15 | `session.info` | — | **missing** | No wire backend. |
| 16 | `session.most_recent` | — | **missing** | Derivable from `sessions.list` limit 1. |
| 17 | `session.title` | — | **missing** | No wire backend. |
| 18 | `session.save` | — | **missing** | No wire backend. |
| 19 | `session.undo` | — | **missing** | No wire backend. |
| 20 | `session.usage` | — | **missing** | CLI-only (`oc usage sessions`). |
| 21 | `session.status` | — | **missing** | No wire backend. |
| 22 | `session.compress` | — | **missing** | `/compress` slash exists; no dedicated RPC. |
| 23 | `session.branch` | — | **missing** | Fork is CLI-only. |
| 24 | `session.close` | — | **missing** | No wire backend. |
| 25 | `completion` | — | **missing** | Tab-completion RPC. No wire backend. |
| 26 | `config.get` | — | **missing** | REST-only (`dashboard/routes/config.py`). |
| 27 | `config.full` | — | **missing** | REST-only. |
| 28 | `config.mtime` | — | **missing** | REST-only. |
| 29 | `config.set` | — | **missing** | REST-only. |
| 30 | `setup.status` | — | **missing** | No wire backend. |
| 31 | `model.options` | — | **missing** | REST-only (`dashboard/routes/models.py`). |
| 32 | `model.save_key` | — | **missing** | No wire backend. |
| 33 | `model.disconnect` | — | **missing** | No wire backend. |
| 34 | `skills.manage` | — | **missing** | OC `skills.list` is read-only; no manage RPC. |
| 35 | `skills.reload` | — | **missing** | No wire backend. |
| 36 | `shell.exec` | — | **missing** | No wire backend. |
| 37 | `image.attach` | — | **missing** | No wire backend. |
| 38 | `paste.collapse` | — | **missing** | No wire backend. |
| 39 | `input.detect_drop` | — | **missing** | No wire backend. |
| 40 | `clipboard.paste` | — | **missing** | No wire backend. |
| 41 | `terminal.resize` | — | **missing** | No wire backend. |
| 42 | `voice.toggle` | — | **missing** | No wire backend. |
| 43 | `voice.record` | — | **missing** | No wire backend. |
| 44 | `tools.configure` | — | **missing** | No wire backend. |
| 45 | `process.stop` | — | **missing** | No wire backend. |
| 46 | `browser.manage` | — | **missing** | No wire backend. |
| 47 | `rollback.list` | — | **missing** | Checkpoints are CLI-only. |
| 48 | `rollback.diff` | — | **missing** | CLI-only. |
| 49 | `rollback.restore` | — | **missing** | CLI-only. |
| 50 | `prompt.background` | — | **missing** | No wire backend. |
| 51 | `prompt.md` | — | **missing** | No wire backend. |
| 52 | `delegation.status` | — | **missing** | No wire backend. |
| 53 | `delegation.pause` | — | **missing** | No wire backend. |
| 54 | `subagent.interrupt` | — | **missing** | No wire backend. |
| 55 | `spawn_tree.list` | — | **missing** | `oc sessions tree` is CLI-only. |
| 56 | `spawn_tree.load` | — | **missing** | CLI-only. |
| 57 | `spawn_tree.save` | — | **missing** | CLI-only. |

**RPC tally: 57 methods — `direct` 0, `adapter` 7, `missing` 50.**
`missing` rate = **50 / 57 ≈ 88%.**

---

## 4. Event mapping (Hermes → OC)

| Hermes event | OC event | Class | Note |
|---|---|---|---|
| `error` | `error` | adapter | Payload `{message}` vs `{error,detail}`. |
| `message.start` | `turn.begin` | adapter | Semantics overlap, not identical. |
| `message.delta` | `assistant.message` (kind=delta) | adapter | OC has no incremental `rendered` markdown field. |
| `message.complete` | `assistant.message` (kind=final) + `turn.end` | adapter | Hermes carries `usage`; split across two OC events. |
| `tool.start` | `tool.call` | adapter | Field remap. |
| `tool.complete` | `tool.result` | adapter | Hermes carries `inline_diff`,`duration_s`,`summary`. |
| `approval.request` | `permission.request` | adapter | Field remap. |
| `gateway.ready` | — (hello handshake) | adapter | Connection-level; OC's hello covers it loosely. |
| `tool.progress` | — | **missing** | |
| `tool.generating` | — | **missing** | |
| `status.update` | — | **missing** | |
| `thinking.delta` | — | **missing** | OC streams thinking inline. |
| `reasoning.delta` | — | **missing** | |
| `reasoning.available` | — | **missing** | |
| `skin.changed` | — | **missing** | |
| `session.info` | — | **missing** | |
| `voice.status` | — | **missing** | |
| `voice.transcript` | — | **missing** | |
| `browser.progress` | — | **missing** | |
| `clarify.request` | — | **missing** | OC consent surface has only `permission.request`. |
| `sudo.request` | — | **missing** | |
| `secret.request` | — | **missing** | |
| `background.complete` | — | **missing** | |
| `review.summary` | — | **missing** | |
| `subagent.spawn_requested` | — | **missing** | |
| `subagent.start` | — | **missing** | |
| `subagent.thinking` | — | **missing** | |
| `subagent.tool` | — | **missing** | |
| `subagent.progress` | — | **missing** | |
| `subagent.complete` | — | **missing** | |
| `gateway.stderr` | — | **missing** | Hermes spawns its own gateway child; OC's TUI attaches. |
| `gateway.start_timeout` | — | **missing** | Same reason. |
| `gateway.protocol_error` | — | **missing** | Same reason. |

**Event tally: 33 events — `direct` 0, `adapter` 8, `missing` 25.**
`missing` rate = **25 / 33 ≈ 76%.**

OC-only events with no Hermes consumer: `memory.write`,
`evolution.tuning_changed`, `stream.retry`, `profile.swap` — Hermes' TUI
would silently drop these (it has no component for them).

---

## 5. Verdict — GATE FAILED, NO-GO on Approach H

| Surface | Total | `direct` | `adapter` | `missing` | Missing % | Gate (30%) |
|---|---|---|---|---|---|---|
| RPC methods | 57 | 0 | 7 | 50 | **88%** | ❌ FAIL |
| Events | 33 | 0 | 8 | 25 | **76%** | ❌ FAIL |

The spec's M1 gate: *">30% gap → stop-and-escalate."* Both surfaces are
roughly **3× over** the threshold.

### Why this kills Approach H specifically

Approach H / B assumed the work was a **translation adapter** (`M3` in the
plan, sized L / 9-12 days): take OC events, reshape them into Hermes' shape.
That assumption is false. ~75-88% of what Hermes' TUI calls has **no OC wire
backend to translate from**. Closing the gap is not "write an adapter" — it
is:

1. Writing ~50 new `WireServer._dispatch` RPC handlers.
2. Wiring each to a Python backend (some exist as REST/CLI and can be
   re-fronted; many — `voice.*`, `image.attach`, `rollback.*`,
   `spawn_tree.*`, `delegation.*`, `terminal.resize`, `clipboard.paste` —
   need genuinely new server code).
3. Emitting ~25 new server→client events from the agent loop / consent
   gate / subagent runtime.

That is exactly the failure mode the spec's own §4.2 audit named: *"if the
mapping count exceeds 30, this plan is wrong and we should switch to C."*
The count is 50. The plan is wrong.

### What the spec says happens next

Per TUI.md §4.1 and §4.6: **"If M1 reveals the gap is bigger than 30%, the
whole plan reverts to Approach C (build OC's own TUI from scratch in Ink),
~10-14 weeks."** M2-M5 (vendor Hermes, build adapter, rebrand, docs) **do
not start.**

---

## 6. Recommendations (decision required from Saksham)

The spike has done its job: it stopped a 6-8 week effort before milestone 2.
Three honest paths, no hand-waving:

1. **Approach C — build OC's own Ink TUI.** Owns every line, no Hermes
   coupling, ships OC-native surfaces (sandbox/awareness panels). Cost:
   10-14 weeks. The spec already names this as the M1-fail fallback.

2. **Approach E — invest in `oc chat` (Python Rich) instead.** The spec's
   own closing note (TUI.md:522) flags that Saksham's actual daily UI is
   `oc chat`, not `oc tui`. If the TUI is not a real daily surface, the
   Rich CLI is the better-fitted target and Ink can stay minimal. Cost:
   2-3 weeks, lowest risk, single codebase.

3. **Approach Z (TUI.md Part 2) — OpenClaw-inspired slash UX upgrade.** A
   self-contained ~1-week PR against the *current* 200-LOC OC TUI: typed
   slash commands with per-command `getArgumentCompletions`, command
   aliases, multi-level typed-value commands. Independent of the
   vendor/fork decision; a felt improvement either way.

A hybrid is reasonable: **ship Z now** (1 week, certain value) **and decide
C vs E** based on whether the Ink TUI is a surface you actually want to
own long-term.

---

## Appendix — verification trail

- Hermes RPC literals: `grep -rhoE "['\"][a-z_]+\.[a-z_.]+['\"]" ~/.hermes/hermes-agent/ui-tui/src/`
- Hermes events: `~/.hermes/hermes-agent/ui-tui/src/gatewayTypes.ts:459-524`
- OC wire methods: `opencomputer/gateway/wire_server.py::_dispatch` + hello list at `:337-361`
- OC constants: `opencomputer/gateway/protocol.py:61-129`, `protocol_v2.py::METHOD_SCHEMAS`/`EVENT_SCHEMAS`
- OC current TUI client: `opencomputer/ui-tui/dist/gatewayClient.js` (11 method wrappers)
