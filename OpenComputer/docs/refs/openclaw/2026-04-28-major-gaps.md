# OpenClaw vs OpenComputer — Selective Major-Gap Audit

**Date:** 2026-04-28
**Scope:** Only the OpenClaw capabilities that (a) OpenClaw does **materially better** than Hermes/Claude Code, (b) OC doesn't already have a good-enough version of, and (c) fit OC's positioning. **This audit is deliberately narrow.** It is *not* a feature-parity matrix.

**Companion docs (read these for full context, but they are NOT the recommended port list):**
- `docs/refs/openclaw/2026-04-28-deep-feature-survey.md` — full 4455-word OpenClaw feature catalog (117 extensions, mobile/canvas/voice subsystems). Reference material.
- `docs/refs/openclaw/2026-04-28-oc-current-state.md` — fresh OpenComputer snapshot: 27 extensions, 426 test files, 9 in-flight PRs.
- `docs/refs/openclaw/inventory.md` (2026-04-22) — old verdict table; stale.
- `docs/refs/hermes-agent/inventory.md` — Hermes parity tracker.
- `docs/refs/hermes-agent/2026-04-28-major-gaps.md` (commit `02e6db67`) — companion Hermes gap doc. Most "obvious OpenClaw features" are already covered there.

**Ground truth as of writing:** OC `main` `4db74443` (PR #221 Hermes channel megamerge tip). 27 bundled extensions, 426 test files, 9 open PRs (#220-#228) covering Skills Hub, first-class tools, slash commands, provider runtime flags, slash-skill-fallback, TUI bell+editor, Edge TTS, Groq STT.

---

## Selection principle (read first)

OpenComputer already has **most** of OpenClaw's surface, often in better shape: the F1-F6 sub-projects, Layered Awareness L0-L4, Life-Event Detector, plural personas, Tier-S Hermes 28-item port, voice-mode, browser-control, ambient-sensor, native introspection, 14 channel adapters from the Hermes megamerge, auto-skill-evolution, and the Sub-projects A/B/C/D/E ship-gates. The reverse direction — what OC has that OpenClaw doesn't — is at least as long.

The user's directive: **"don't bombard and fill the whole thing with OpenClaw features now. I want you to be very selective. Only pick the ones that truly, truly, really need the OpenClaw upgrade."** This audit applies that filter ruthlessly.

**The four items that pass the filter:**

1. **Multi-agent isolation + channel-binding router** — OpenClaw routes inbound messages to one of N isolated agents inside a single Gateway via `(channel, accountId, peer, parentPeer, guildId, roles, teamId)` deterministic binding match. Hermes uses profile-per-process; OC inherited that and is single-agent-per-Gateway. **OpenClaw does this materially better than Hermes** and OC has no equivalent. The single biggest architectural delta worth porting.

2. **Standing Orders (text-contract for autonomous program authority)** — Declarative `## Program: <name>` blocks in AGENTS.md grant the agent permanent operating authority for autonomous programs (scope + triggers + approval gates + escalation rules). Hermes has cron jobs (job-execution-as-syntax) and OC has the same. **OpenClaw's text-contract DSL is the higher-level abstraction Hermes/OC lack.** Combined with our existing cron, this fills the "I want the agent to OWN this program" gap.

3. **Active Memory (blocking pre-reply recall sub-agent)** — A bounded sub-agent that runs on every eligible reply, queries `memory_search`/`memory_get`, and injects the result as a hidden untrusted prefix. **OC has `RecallTool` (reactive — model decides to call it) and `reviewer.py` (post-response).** Neither is the *blocking gate* OpenClaw runs proactively before every reply. The blocking pattern materially changes how often relevant memories surface in casual chat — Recall as a tool is "agent remembered to look up"; Active Memory is "Look-up runs by default."

4. **Block streaming chunker + `humanDelay`** — Channel deltas arrive at human-readable cadence: paragraph-boundary first, then newline, then sentence, then whitespace; never split inside code fences; idle-coalescing merges quick chunks; randomized 800-2500ms `humanDelay` between blocks. OC ships token-stream raw to channels — on Telegram especially the result is robotic. Hermes streams in a similar raw pattern. **OpenClaw is uniquely sophisticated here.**

That's it. **Everything else is in the "Deliberately not porting" section below**, with reasons.

---

## What we are deliberately NOT porting (and why)

This list is longer than the "porting" list on purpose — it's how we show we rejected items intentionally rather than missed them.

### From OpenClaw — present, but NOT a gap worth porting

| Feature | Why not |
|---|---|
| **117 extensions (50 providers, 25 channels, 13 tools, etc.)** | Most providers are already covered by `litellm` or shipped natively (anthropic, openai, aws-bedrock). Most channels are niche (nostr, twitch, irc, qqbot, line, zalo, wechat, feishu, voice-call SIP). The ones we needed (matrix, mattermost, signal, slack, whatsapp, email, webhook, sms, imessage, homeassistant) shipped via the Hermes megamerge in PR #221. We **don't bulk-port plugin breadth.** |
| **A2UI Live Canvas** | CLAUDE.md §5: canvas rendering is wont-do. OC is CLI-first. |
| **Mobile companion apps (iOS/Android/macOS menu-bar)** | CLAUDE.md §5: native mobile apps are wont-do. |
| **Voice Wake + Talk Mode + Voice Call (SIP)** | CLAUDE.md §5: voice wake-word is wont-do. Voice-mode (PR #199) covers continuous push-to-talk; that was the bound. SIP voice-call is unrelated infra. |
| **Codex / acpx / opencode external-CLI-as-harness** | OC *is* the agent harness. Bridging external CLIs through us is an anti-pattern; we'd be doing OpenClaw's positioning, not ours. |
| **`mcporter` external MCP bridge** | Different vision. OC bundles MCP via `opencomputer/mcp/`. Not a gap; a positioning choice. |
| **Manifest-first plugin model (`openclaw.plugin.json`)** | OC's `register(api)` + `PluginManifest` is cleaner for Python. JSON manifests duplicate what `PluginManifest` already encodes. |
| **TypeBox/Swift codegen pipeline** | TypeScript-tied; only relevant if we build mobile apps (we won't). |
| **Sparkle update appcast** | OC uses `pip install -U`. |
| **Lobster (typed workflow tool with resumable approvals)** | Niche workflow primitive. OC's `ExitPlanMode` + `AskUserQuestion` cover the common cases. Reopen only if a real resumable-workflow user need surfaces. |
| **TaskFlow declarative pipelines** | High scope-creep risk. No clear user demand. Skip. |
| **Background Tasks ledger (full state machine)** | OC has `opencomputer/tasks/` with runtime + store. OpenClaw's polished state machine is incremental, not strategic. Defer. |
| **Diagnostics OTEL plugin** | Useful but observability is a small win. Not strategic. Defer until ops demand. |
| **Skill Workshop auto-capture** | OC's auto-skill-evolution (PR #193 + #204) covers the same ground with a SessionMetrics adapter. Not a gap. |
| **Memory plugins (memory-lancedb, memory-wiki)** | OC has Honcho default + episodic + declarative + procedural + dreaming.py 495 LOC. Adding LanceDB/wiki vaults is plugin breadth, not a strategic gap. |
| **ACP bridge expansion (loadSession, per-session MCP, tool streaming)** | OC's `acp/` covers core flow per claude-code parity. Expansion is gated on IDE adoption demand. Park. |
| **Sandbox-browser variant (Chromium + xvfb + VNC)** | Marginal — adding a Dockerfile mostly. Defer. |
| **Multi-stage approval rendering (terminal/web/channel)** | F1 consent ships. Multi-renderer is polish; reopen later. |
| **Auth monitoring loop** | `doctor.py` covers the static check. Periodic auto-monitor is marginal. |
| **Gmail Pub/Sub trigger** | Niche; covered by webhook + cron if needed. |
| **Anti-loop / loop detection** | Useful but small. Add only when a loop-degenerate session bites. |
| **OSC8 hyperlinks in TUI** | Polish. Defer. |
| **Sandbox tiers via Docker** | OC has 6 sandbox runners (`auto/docker/linux/macos/none/ssh`). Already at parity. |
| **Cron job session-isolation modes (`main\|isolated\|current\|session:<id>`)** | Polish on existing cron. Defer. |
| **Hook taxonomy expansion (18 events vs OC's 12)** | OC's 12 events plus the precise emit points cover ~95% of practical use. Adding 6 more events is bookkeeping, not strategic. **EXCEPTION:** if we ship Active Memory (#3 above), we need a `before_agent_reply` hook event (or reuse `PreLLMCall`). That single event lands inside the Active Memory PR. |
| **Inbound queue modes (5 policies per channel)** | Hermes has `/queue` + `/steer` patterns we can port (covered in Hermes Tier 2.A continuation work). Per-channel mode selection is incremental; not strategic. |
| **Replay sanitization (assistant-text strip-rules)** | Small win. Can be a single 50-line PR when needed. Not strategic. |

### From Hermes — already shipped, no follow-up

(These are documented in the companion Hermes gap doc; listed here only so a synthesizer doesn't double-count them.)

- Skills Hub MVP — PR #220 in flight.
- First-class generative tools — PR #222 in flight.
- 6 self-contained slash commands — PR #223 in flight.
- Provider runtime flags — PR #224 in flight.
- `/<skill-name>` auto-dispatch — PR #225 in flight.
- TUI bell + external editor — PR #226 in flight.
- Edge TTS — PR #227 in flight.
- Groq STT — PR #228 in flight.
- Hermes channel feature port (Tier 1+2+3 less Matrix E2EE) — PR #221 merged.
- Voice mode, browser control, layered awareness V2.B/V2.C, affect work A/B/C, passive education v1+v2, ambient sensor, native introspection, Tier-S 28-item port, Sub-projects A/B/C/D/E/F1, TUI Phase 1+2.

---

## The four real gaps — detailed

### Gap 1 — Multi-agent isolation + channel-binding router

**Verdict:** Port. **Tier:** 1.A. **Effort:** XL (multi-PR, 2-3 weeks). **Strategic:** Yes — this is the headline architectural delta and a prerequisite for several follow-ons.

**Why OpenClaw does this materially better than Hermes:**

Hermes ships profile-per-process: one Gateway daemon, one set of channels, one agent identity. To run "work-agent" + "home-agent" you spawn two Hermes processes with two profiles. Channel-binding to agent doesn't exist as a config concept.

OpenClaw routes inbound messages inside *one* Gateway daemon to one of N truly isolated agents using a deterministic binding rule:

```yaml
# OpenClaw's ~/.openclaw/config.yaml shape
agents:
  defaults:
    agentDir: "{home}/agents/{agentId}"
  bindings:
    - { channel: slack, accountId: "T01ABC", agent: work }
    - { channel: slack, accountId: "T01ABC", peer: "U99XYZ…", agent: home }   # most-specific wins
    - { channel: telegram, peer: "@dad", agent: home }
```

Per-agent state lives at `~/.openclaw/agents/<id>/` — own auth-profiles, sessions, workspace, AGENTS.md, SOUL.md, model registry, skills allowlist. Each agent is a fully-scoped brain.

Match key: `(channel, accountId, peer, parentPeer, guildId, roles, teamId)`. Most-specific match wins (count of specified keys is the score; ties broken by config order). Deterministic. Documented at `docs/concepts/multi-agent.md`.

**Why OC needs it:** Today "I have a work agent and a home agent both connected to one Slack workspace via different DM channels" requires running two `oc gateway` processes with two profiles — and even then, the gateway can't route based on `peer` or `roles`. With multi-agent isolation + bindings, that scenario is one-Gateway, two-agents, one-config.

**Scope for OC port (the plan in `docs/superpowers/plans/2026-04-28-openclaw-tier1.md`):**

1. New SDK types in `plugin_sdk/multi_agent.py`: `AgentDescriptor`, `BindingRule`, `BindingMatchKey` (frozen dataclasses).
2. New core module `opencomputer/agents_runtime/` (parallel to `agent/` — namespace pluralization avoids collision) with `AgentRouter`, `AgentRegistry`, per-agent state-dir resolver.
3. New CLI subapp `oc agents` (NOT `oc agent` — `agent` is already overloaded; `agents` plural is the standard). Subcommands: `list / create / show / delete / use / bindings list / add / remove / test`.
4. New config block under `agents:` in `~/.opencomputer/<profile>/config.yaml` matching the OpenClaw shape.
5. Gateway `dispatch.py` change: resolve binding key → `agentId` via `AgentRouter.resolve_binding(...)`.
6. SessionDB schema: add `agent_id TEXT NOT NULL DEFAULT 'default'` column. Migration back-fills existing rows.
7. Skills loader: each agent gets its own `~/.opencomputer/<profile>/agents/<id>/skills/` plus shared `~/.opencomputer/<profile>/skills/` root. Allowlist filter applied per agent.
8. Auth profiles: per-agent `auth-profiles.json` under `~/.opencomputer/<profile>/agents/<id>/auth/`.
9. Channel adapters that today call `dispatch.handle_message_event(event)` need to pass `event.agent_id` (resolved at gateway entry).
10. `oc chat` retains current behavior (single agent, profile is the boundary). New `oc chat --agent <id>` selects an agent within a profile.

**Test surface:**
- Unit: `BindingRule.match_score()` covers each key combination; deterministic ordering.
- Integration: gateway routes a Slack message in a workspace to two different agents based on `peer`.
- Migration: existing SessionDB rows survive the `agent_id` schema change; existing single-agent profiles continue to work.

**OpenClaw upstream refs (for the porter):**
- `docs/concepts/multi-agent.md`
- `docs/concepts/delegate-architecture.md` (related but separate concept — out of scope)
- `src/agents/auth-profiles*` (auth-profile-per-agent rotation, cooldown, doctor integration)

---

### Gap 2 — Standing Orders + cron integration

**Verdict:** Port. **Tier:** 1.B. **Effort:** L (5-6 days, single PR). **Strategic:** Yes.

**Why OpenClaw does this materially better than Hermes:**

Hermes has cron (`hermes cron`) — schedule a job, fire at a time, run a tool. OC ships the same in `opencomputer/cron/`.

OpenClaw layers a higher-level abstraction on top: **Standing Orders** are declarative `## Program: <name>` blocks in AGENTS.md (or sibling `standing-orders.md`) granting the agent **permanent operating authority** for autonomous programs. Each program declares:

```markdown
## Program: weekly-report

**Scope:** compose + send the weekly engineering report
**Triggers:** every Friday at 17:00 (cron); when label `report-ready` is added in Linear (event)
**Approval gates:** none for compose; ANY scheduled send requires prior `/standing-orders test weekly-report` confirmation
**Escalation rules:** if data sources fail health check, post to #eng-ops and stop
```

Combined with cron + an event-trigger lane, this is the "I authorize the agent to OWN this program" abstraction. Hermes/OC have job-execution-as-syntax (`hermes cron schedule "..." -- <tool>`) but not the program-as-grant abstraction.

**Why OC needs it:** Standing Orders are how a user expresses ongoing trust. Without them, every autonomous run is a new approval. With them, the program-level grant is the unit of trust, and per-run approval is silent within scope.

**Scope for OC port:**

1. New parser `opencomputer/standing_orders/parser.py` reads AGENTS.md (or `standing-orders.md` referenced from it) and extracts `Program(name, scope, triggers, approval_gates, escalation_rules)` dataclasses. Format mirrors OpenClaw — markdown sections + bullet lists.
2. New runtime `opencomputer/standing_orders/runtime.py` wires programs to:
   - Cron triggers (`schedule:` or `every:` field) — registered into `opencomputer/cron/`.
   - Event triggers (`on:` field — `gmail:label-added`, `filewatch:/path`) — webhook-driven.
   - Agent loop integration — when a program is "active," its grant is added to the system prompt as a permanent-authority block.
3. CLI: `oc standing-orders list / show <name> / test <name> / disable <name>`.
4. **Approval gates:** programs declare `approves:` (tools that bypass per-call approval inside this program's scope) and `requires_approval:` (tools that always escalate even within program). The agent loop's hook engine reads these per active program.
5. SOFT dep on Gap 1 (multi-agent): per-agent Standing Orders make the most sense in a multi-agent world. Single-agent users get one Standing-Orders file per profile.

**Test surface:**
- Parser: nested bullets, missing scope, malformed schedule, no triggers, multiple programs in one file.
- Runtime: cron triggers fire the agent; approval-gate-list correctly bypasses approval for whitelisted tools and not for others.
- Lifecycle: disabled program does not fire; deleted program is unloaded on next reload.

**OpenClaw upstream refs:**
- `docs/automation/standing-orders.md`
- `docs/automation/cron-jobs.md`

**What we're explicitly NOT taking from OpenClaw's automation surface:**

- **Heartbeat lane** (separate always-on agent ticker decoupled from cron) — `docs/automation/cron-vs-heartbeat.md`. We have cron; an extra "agent ticks at user-configured cadence" lane is incremental. Defer.
- **TaskFlow declarative pipelines** — high scope-creep. Defer.
- **Background Tasks ledger** (full state machine + 7d retention + audit). OC's `tasks/` runtime+store is sufficient for the immediate need. Defer.

---

### Gap 3 — Active Memory (blocking pre-reply recall sub-agent)

**Verdict:** Port. **Tier:** 1.C. **Effort:** M (2-3 days, single PR). **Strategic:** Yes.

**Why OpenClaw does this materially better than Hermes/OC:**

OC has two memory-recall paths:
- **`RecallTool`** — model decides to call it, looks up episodic memory by query. Reactive.
- **`reviewer.py`** (post-response) — runs after a reply lands, captures lessons from the turn. Post-hoc.

Neither runs *before* the reply as a blocking gate. OpenClaw's `extensions/active-memory/` is a bounded pre-reply sub-agent (`docs/concepts/active-memory.md`):

- Hooks into `before_agent_reply` event.
- Bounded by `timeoutMs` (default 8000ms).
- Has access to two tools only: `memory_search(query, limit)` and `memory_get(id)`.
- Returns JSON `{"action": "inject"|"skip", "summary": "..."}`.
- On `inject`, prepends a fenced `<relevant-memories>` block to the system message.
- Tunable prompt-styles (`balanced`/`strict`/`contextual`/`recall-heavy`/`precision-heavy`/`preference-only`) tune the precision/recall trade-off.
- Per-chat-type allowlist (DM only, group only, all).
- Caching: identical (chat-id, last-user-msg-hash) within `cacheTtlMs` reuse the prior decision.

**Why this materially changes things:** Recall-as-tool only fires when the model decides to look up — which it doesn't on most casual replies. Active Memory always fires on eligible replies, returns nothing if nothing relevant, and silently improves continuity. Different mental model: Recall is "agent remembered to look up"; Active Memory is "look-up runs by default."

**Scope for OC port:**

1. New extension `extensions/active-memory/` with `plugin.py` registering hook for `BeforeAgentReply` event.
2. **Required: add `BeforeAgentReply` hook event** (single new event in `plugin_sdk/hooks.py::ALL_HOOK_EVENTS`). Emit point in `agent/loop.py` after prompt-build, before provider call. This is the only hook-taxonomy expansion we need — not a wholesale 6-event addition.
3. Plugin config schema mirrors OpenClaw: `enabled`, `model`, `modelFallback`, `allowedChatTypes`, `thinking`, `timeoutMs`, `queryMode` (message/recent/full), `promptStyle`, `recentUserTurns`, `recentAssistantTurns`, `cacheTtlMs`.
4. Hook receives `HookContext(messages, runtime, ...)`, runs sub-agent with prompt-style template, returns `{"action": "inject"|"skip", "summary": "..."}`. On `inject`, prepend fenced `<relevant-memories>` block to system message.
5. Sub-agent has `memory_search(query, limit)` and `memory_get(id)` only. Bounded by `timeoutMs`.
6. Caching: identical lookups reuse the prior decision within `cacheTtlMs`.
7. Optional `persistTranscripts` writes the sub-agent transcript to `agentDir/active-memory/YYYY-MM-DD.md`.
8. Slash command: `/active-memory pause / resume / status`.

**Test surface:**
- Hook fires on `BeforeAgentReply` and not on any other event.
- Sub-agent runs in `<timeoutMs>` and returns; on timeout the main reply proceeds without injection (fail-open).
- Cache hit returns prior decision without re-invoking.
- `allowedChatTypes` filter respects the chat type (DM/group/channel).

**OpenClaw upstream refs:**
- `extensions/active-memory/openclaw.plugin.json`
- `docs/concepts/active-memory.md`
- `extensions/active-memory/src/index.ts`

---

### Gap 4 — Block streaming chunker + `humanDelay`

**Verdict:** Port. **Tier:** 1.D. **Effort:** M (2 days, single PR). **Strategic:** Yes for messaging-channel UX.

**Why OpenClaw does this materially better than Hermes/OC:**

Both Hermes and OC stream provider tokens directly to channel `send()`. On Telegram especially, the result is robotic — each token-delta becomes an edit, and rapid edits look machine-generated.

OpenClaw's chunker:
- **Block-level boundaries**: paragraph-first, then newline, then sentence, then whitespace. **Never** split inside a code fence.
- **Idle-coalescing**: chunks arriving within 100ms merge into one delivery.
- **Min/max bounds**: small chunks (< `minChars`) buffer; large chunks (≥ `maxChars`) split at the best boundary.
- **`humanDelay`**: between blocks, randomized pause (default 800-2500ms) makes replies feel typed-by-a-person rather than stream-flushed-by-an-LLM.

OC's `voice-mode` already has some chunking primitives at `extensions/voice-mode/streaming.py`, but the chunker is generic and not channel-adapter-wired.

**Scope for OC port:**

1. New `plugin_sdk/streaming/block_chunker.py` with `BlockChunker(min_chars, max_chars, prefer_boundaries=("paragraph","newline","sentence","whitespace"), never_split_fences=True, idle_coalesce_ms=100, human_delay_min_ms=800, human_delay_max_ms=2500)`.
2. Channel adapters opt in by wrapping their `on_delta` handler in `BlockChunker.feed(delta) -> Iterator[Block]`. Default off — TUI streams raw to keep responsiveness; channel adapters opt in via config.
3. Per-channel config in `~/.opencomputer/<profile>/config.yaml`:
   ```yaml
   channels:
     telegram:
       streaming:
         block_chunker: true
         human_delay_min_ms: 800
         human_delay_max_ms: 2500
   ```

**Test surface:**
- Single-block input yields single block.
- Multi-paragraph input splits at paragraph boundaries first.
- Code-fence input never splits inside the fence even if it exceeds `maxChars`.
- Rapid deltas within 100ms merge.
- `humanDelay` produces a pause within `[min_ms, max_ms]` (statistical test).

**OpenClaw upstream refs:**
- `docs/concepts/streaming.md`

---

## Top picks (ordered for the next ship-wave)

| # | Title | Tier | Effort | Notes |
|---|---|---|---|---|
| 1 | Multi-agent isolation + channel-binding router | 1.A | XL | Multi-PR. Headline. Soft-prereq for #2. |
| 2 | Standing Orders + cron integration | 1.B | L | Layers on top of cron. Soft-prereq is #1 for full effect. |
| 3 | Active Memory blocking pre-reply sub-agent | 1.C | M | Standalone. Adds one hook event (`BeforeAgentReply`). |
| 4 | Block streaming chunker + `humanDelay` | 1.D | M | Standalone. Per-channel opt-in. |

**Plan-of-record for next session:** `docs/superpowers/plans/2026-04-28-openclaw-tier1.md` covers all four with full TDD task decomposition and Phase 0 pre-flight verification, with #1 (multi-agent) the deepest. Ships independently.

---

## End of audit

**Length:** ~280 lines (deliberately ~⅓ the size of the previous draft — selectivity is the point).

**Resume command for next session:**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git checkout main && git pull
# Read in this order:
#   1. docs/refs/openclaw/2026-04-28-major-gaps.md           (this file — 4 picks)
#   2. docs/refs/openclaw/2026-04-28-deep-feature-survey.md  (reference catalog)
#   3. docs/refs/openclaw/2026-04-28-oc-current-state.md     (where we are)
#   4. docs/superpowers/plans/2026-04-28-openclaw-tier1.md   (the executable plan)
# Then execute via:
#   /executing-plans      (sequential)
#   /subagent-driven-development (parallel agents per task)
```
