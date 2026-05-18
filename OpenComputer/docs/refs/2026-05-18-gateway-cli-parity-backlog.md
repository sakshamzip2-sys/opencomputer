# Gateway-vs-CLI Parity — Remaining Work Backlog

Date: 2026-05-18
Owner: Saksham
Source of truth for the gap catalogue:
`docs/superpowers/specs/2026-05-18-gateway-vs-cli-extended-gap/EXTENDED-GAP-ANALYSIS.md`

This file is the **execution backlog** — every pending task to bring the
gateway (and therefore every connector: Telegram, Discord, Slack,
Matrix, …) to full parity with the CLI, plus the dynamic thinking-budget
work. It is kept honest: shipped items are marked, remaining items are
sized and prioritised, and nothing is hand-waved.

---

## 1. Status snapshot (2026-05-18)

**Shipped to `main` this cycle — 10 gaps + thinking-budget bump:**

| Gap | What | PR |
|-----|------|----|
| A1 | Live streaming — replies type out as they generate | #652 |
| A2 | `/plan on\|off` — per-chat plan mode | #648 |
| A3 | `gateway_safe` slash commands actually run on the gateway | #648 |
| A6 | Per-chat working directory for file / Bash tools | #648 |
| A7 | One-line session banner (opt-in) | #648 |
| A8 | `/handoff` — persistent profile swap on the gateway | #648 |
| A9 | Binding-level `queue_mode` | #648 |
| B3 | Channel-capability block in the system prompt | #656 |
| D1 | `/which` — resolution-chain inspector | #656 |
| D2 | `/tools` — tool-surface inspector | #656 |
| — | Thinking-budget defaults +1 tier + dynamic-budget design | #655 |

**Result:** a connector went from a "dumb relay" to ~80% of the CLI
experience. The remaining work below is mostly P2/P3 polish plus a few
genuine architectural pieces.

**Legend** — Effort: XS (hours) · S (1-3 d) · M (3-7 d) · L (1-3 w).
Priority: P1 (next) · P2 (later) · P3 (niche).

---

## 2. Wave 2 — finish the affordance layer (P1)

| ID | Task | Effort | Deps | Notes |
|----|------|--------|------|-------|
| B1 | `max_tokens` auto-continue — detect `stop_reason="max_tokens"` on a pure-text reply and emit a continuation call, concatenate. Today only tool-use replies auto-continue. | S | — | Core agent-loop change; needs care + TDD. |
| C3 | Per-chat persona **register** (`warm`/`task`/`reflective`) — a binding field injected into the persona slot, bypassing the platform classifier. | S→M | — | Investigation found **no clean seam** distinct from M3's `persona_id_override`; needs a small design step first (define what each register means as an overlay). |
| A4 | `/cancel` + `/stop` aliases that interrupt the in-flight turn; per-chat `interrupt` queue-mode default for chat platforms. (Binding `queue_mode` already shipped via A9.) | S | A3✓ | Queue-manager `interrupt` mode already exists; this is the alias + default. |

---

## 3. Wave 3 — output / display polish (P2)

| ID | Task | Effort | Deps | Notes |
|----|------|--------|------|-------|
| B2 | Reasoning visibility on Tier-1 connectors — default `show_reasoning: True` on telegram/discord; `/reasoning on\|off` tagged `gateway_safe`. | S | A3✓ | — |
| B4 | Per-platform Markdown rendering — central `gateway/render.py` with per-adapter escape/format rules (Telegram MDV2, Discord, Slack, IRC, SMS). | M | B3✓ | New module. |
| B5 | `"compact"` tool-progress mode — one-liner per tool (`▸ Bash · 0.4s · exit 0`); default on Tier 1. | S | — | — |
| B6 / F1 | Outbound media interceptor — route `Write`/`ImageGenerate` results through `adapter.send_image` / `send_document` when the capability supports it. | M | B3✓ | New tool-result dispatch layer. |
| F3 | Auto-skill routing for inbound PDF / docx / pptx / audio — mimetype-detect and inject extracted text into the next user message. | M | — | — |
| C4 | `/resume <session-id-prefix>` — rebind a gateway chat to a different session's history. | S | A3✓ | — |
| D3 | `/model <id>` — per-session model override on the gateway. | S | — | Port the `cli_model_picker` resolver. |
| E2 | `/prompt` — return a redacted summary of the last turn's rendered system prompt. | S | A3✓ | CLI parity for `oc context show`. |
| G6 | `AgentRouter` cache invalidation on `oc config reload` — cached per-profile loops currently hold stale config until process restart. | S | — | Verify profile-rebind hooks fire. |
| C1.1 | `/pin <message_id>` — keep a past turn verbatim through compaction. | S | A3✓ | — |
| I8 | Inject ambient context (foreground app, recent files) into the gateway turn — parity with the CLI; the persona classifier currently sees less on the gateway. | S | — | Ambient daemon already writes `ambient/state.json`. |

---

## 4. Wave 4 — niche / per-platform (P3)

Mostly XS–S; each is a `gateway_safe` slash command or a small plumbing
fix. The A3 mechanism makes the slash commands cheap.

| ID | Task | Effort |
|----|------|--------|
| E1 | `gateway.diagnose.status` wire RPC + `/diagnose` slash | S |
| E3 | `/audit` — last N tool calls of the session from `audit.db` | XS–S |
| E4 | Boot-time "sanitised N stale messages" notification to the home channel | XS |
| F2 | `display.voice_response` — speak the reply back when input was voice | S |
| F4 | Auto `VisionAnalyze` on inbound images; inject the description (opt-in) | S |
| F5 | Show the Whisper transcription to the user (`[transcribed: "…"]`) | XS |
| C5 | `/mirrors` — list recent cross-session mirror entries for the chat | XS |
| C6 | `/boot` — show the last `BOOT.md` run output + timestamp | XS |
| D4 | Document the per-profile MCP-fleet routing trade-off | XS (docs) |
| D5 | Document chat-scoped vs profile-scoped `MEMORY.md`/`USER.md` | XS (docs) |
| D6 | `/privacy` — show PII-redaction status + how to enable | XS |
| G4 | Version the `ChannelCapabilities` flag enum | XS |
| G7 | Test: every `HookEvent` that fires on CLI also fires on gateway | S |
| G8 | Per-message audit-chain integration for the cross-process `outgoing_queue` | S |
| I1 | Periodic gateway preflight re-check (poll for stolen polling slot) | S |
| I2 | `/sethome` + `/whoami` slash commands for cross-channel delivery | S |
| I3 | `/channels` — list known channels with friendly names | XS |
| I4 | Surface the active reset policy via `/status` | XS |
| I5 | Warn on first redaction-enabled boot if the PII salt is not backed up | XS |
| I6 | Exponential backoff + N retries in `outgoing_drainer` before `mark_failed` | S |
| H1–H8 | Per-platform polish (Telegram MDV2 edge cases, Discord embeds, WhatsApp re-auth notice, …) — several are "by design", listed for completeness | varies |

---

## 5. Wave 5 — foundational / architectural (L)

These are real engineering, each its own focused spec + PR. Do NOT
bundle.

| ID | Task | Effort | Notes |
|----|------|--------|-------|
| A5 | Async-consent state machine — release the per-chat lock during a tool-approval round-trip; snapshot/resume turn state. | L (1-2 w) | `AgentLoop.snapshot()` / `resume_from_snapshot()` do **not** exist yet — real new infrastructure. |
| G1 | Unified `build_agent_loop(profile_home, source, **kwargs)` — collapse the 4 current `AgentLoop` construction sites (cli, factory, router, wire). | L (2-3 w) | Foundational; makes every later fix cheaper. |
| G2 | `build_runtime_context(profile_home, source, slash_state, …)` — mirror of G1 for `RuntimeContext`. | S (after G1) | Depends on G1. |
| G3 | Behavioral parity test harness — run 20 canonical prompts through CLI + each adapter (mocked), diff response-length / tool-calls / key terms, fail CI on >25% divergence. | L (~2 w) | New test surface; prevents regression of all the above. |
| C1.2 | Compaction-triggered MEMORY writes — extract durable facts to `MEMORY.md` before each compaction. | M | New dreaming-v2 trigger. |
| C1.3 | Auto-fork at N turns — `/auto-fork on` forks a long session, inheriting MEMORY. | M | — |

---

## 6. Dynamic thinking budget

Design: `docs/superpowers/specs/2026-05-18-dynamic-thinking-budget-design.md`
(presented + approved as a design; build pending).

| ID | Task | Effort | Notes |
|----|------|--------|-------|
| TB-M1 | `effort_signal.py` — pure scorer: prompt complexity, user cues, retry-after-failure, prior tool depth → effort delta. | M | The real work; fully unit-testable. |
| TB-M2 | Dynamic layer in `effort_policy` — apply the scorer delta to the static default, asymmetric (eager up, reluctant down). `/reasoning auto` enables it. | S | Additive; static path unchanged when off. |
| TB-M3 | Transparency — show the chosen effort level (footer / `/reasoning status`). | S | — |
| TB-M4 | Control — `/reasoning auto`, per-chat pin (runtime-state), per-connector floor/ceiling (binding fields). | S | Reuses the A2 + A6 patterns. |
| TB-M5 | Depleting per-connector daily thinking-token budget feeding M4's ceiling. | M | Deferred until real demand. |

---

## 7. Honest floor — will NOT fully close

Named by the gap-analysis spec itself (Section J). Do not spend
budget chasing 100% here:

- **J1** — chat medium vs keyboard medium. The register difference
  ("asking a friend while walking" vs "two people at a terminal") is
  driven by the medium; no flag changes it.
- **J2** — gateway sessions are long (months); CLI sessions are short.
  Compaction over long sessions loses specificity. C1.* mitigates,
  does not eliminate.
- **J3** — terminal rendering is fundamentally richer than chat (Rich
  tables, syntax-highlighted diffs, live progress bars). The ceiling is
  "as good as chat can be."

---

## 8. Recommended execution order

1. **Wave 2 finish** (B1, C3, A4) — completes the affordance layer.
2. **G1 + G2** — the unified loop builder. Foundational; every later
   fix gets cheaper and the parity harness (G3) needs a stable seam.
3. **G3** — behavioral parity harness. Locks in everything shipped so
   far against regression before piling on more.
4. **Wave 3** — output polish, now cheaper on top of G1.
5. **Dynamic thinking budget** TB-M1→M4.
6. **A5** — async consent (its own spec; needs the snapshot infra).
7. **Wave 4** — niche items, batched a few per PR.

Each wave = one focused, fully-tested PR with green CI, the same way
A1–A9 / B3 / D1 / D2 shipped. Never a multi-feature dump.
