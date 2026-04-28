# OpenClaw Tier 1 Port — Design Spec (Final, 8-pick scope)

**Date:** 2026-04-28
**Author:** Claude (current session)
**Status:** Approved-pending-audit; 8-pick scope after evidence-based gap analysis
**Supersedes:** PR #229's original 4-pick plan + this spec's earlier 3-pick draft

---

## 1. Goal

Port from OpenClaw the genuine remaining capability gaps in OC, after factoring in:

- The Hermes Tier 1+2+3 megamerge (PR #221, on `main` as `4db74443`)
- The 12 open Hermes PRs (#220–#233) that will land soon
- The 6 not-yet-started Hermes tail items (`@filepath`, `--worktree`, Tirith, `/update`, `/restart`, `/debug`)
- archit-2's runtime PII redaction work (PR #230)
- All earlier OC + OpenClaw imports (13 architectural patterns + 13 explicit ports)

**Filter:** truly required *or* low-cost-high-value gap that survives our existing surface.

## 2. Why an 8-pick scope (not 4, not 3)

Two parallel explorers ran:

- **OC↔OpenClaw map** (sonnet): confirmed OpenClaw is *architectural backbone* of OC — SDK boundary, manifest schema, two-phase discovery, security checks, doctor surface, wire protocol, MCP scaffolder. ~13 modules cite OpenClaw as origin.
- **OC shipped checklist** (sonnet): confirmed which OpenClaw capabilities are already in OC vs which are still missing.

The previous 4-pick (multi-agent isolation, Standing Orders, Active Memory, block chunker) was sound but two of its picks (multi-agent + Standing Orders) failed the "truly required" filter for OC's single-user-CLI positioning. Removing them and surfacing 6 small-but-real gaps gives a better-balanced 8-pick scope.

## 3. The 8 picks

All Phase 1. All independent. All parallel-safe. Each ships as one PR.

| # | Pick | Effort | Why it survives the filter | Conflict risk |
|---|------|--------|-----------------------------|---------------|
| **1.A** | **Block streaming chunker + humanDelay** | M (~2d) | `plugin_sdk/streaming/` doesn't exist; only `cli_ui/streaming.py` for TUI. Telegram robotic-streaming is real pain. | None |
| **1.B** | **Active Memory pre-reply sub-agent** | M (~2d) | Partial only — `vibe_classifier`/`recall_synthesizer`/`reviewer`/`dreaming` exist but no pre-reply blocking gate. RecallTool is reactive (model-decides); pre-reply blocking is proactive. Reuses existing `PreLLMCall` hook (additive `mutable_messages` field on HookContext). | None |
| **1.C** | **Anti-loop / repetition detector** | S (~1d) | Partial only — delegate `MAX_DEPTH` + `strict.yaml` loop budget exist, but no per-call repetition detector. Cheap, real safety win. | None |
| **1.D** | **Replay sanitization (assistant-text strip-rules)** | S (~half day) | OpenClaw strips stale assistant text on catch-up; OC `gateway/dispatch.py` doesn't. Small but unique. | None |
| **1.E** | **Auth profile rotation cooldown + auto-monitor** | S (~1d) | Credential pool rotates on hard fail (Hermes H2, `50aed81d`), but no time-based cooldown + no periodic doctor monitor. | None |
| **1.F** | **Sessions-* tools (spawn / send / list / history / status)** | M (~2-3d) | `/resume` slash exists but no programmatic *tool* for the agent to manage parallel sessions. Inventory lists as "port." | None |
| **1.G** | **Clarify_tool (auto-clarify ambiguous prompt)** | S (~half day) | OpenClaw has it; OC has `AskUserQuestion` (manual) but no auto-trigger when prompt is ambiguous. | None |
| **1.H** | **Send_message_tool (cross-platform programmatic send)** | S (~half day) | Inventory lists as "port"; OC has `outgoing_queue` infra (PR #221) but no tool wrapper for the agent to send. | None |

**Total Phase 1 cost:** ~10-11 engineering days. All M-or-smaller.

## 4. Picks — detailed

### 1.A — Block streaming chunker + `humanDelay`

**The gap.** OC's channel adapters wrap provider stream-deltas straight into adapter `send()`. On Telegram, every token-delta becomes a Bot API edit, producing flickering robotic stream. Discord and Slack share the anti-pattern. There is **no chunker in `plugin_sdk/streaming/`** today.

**Scope:**
- New `plugin_sdk/streaming/__init__.py` package + `block_chunker.py` with `BlockChunker(min_chars=80, max_chars=1500, prefer_boundaries=("paragraph","newline","sentence","whitespace"), never_split_fences=True, idle_coalesce_ms=100, human_delay_min_ms=800, human_delay_max_ms=2500)`.
- API: `feed(text_delta) -> list[Block]`, `flush() -> list[Block]`, `human_delay() -> float` (seconds = `random.uniform(min, max) / 1000`).
- Channel adapters opt in via `~/.opencomputer/<profile>/config.yaml::channels.<name>.streaming.block_chunker: true`.
- Default: OFF for TUI; user-opted ON for telegram/discord/slack/matrix/mattermost.
- Wrapper helper `BaseChannelAdapter._maybe_chunk_delta()` so each adapter wraps in one line.
- **Never splits inside fenced code blocks.** Fence-depth tracking; emit only up to start of unclosed fence.

**Tests:** paragraph-first split; never-split-inside-fence; idle-coalesce; min/max bounds; humanDelay random within range (fixed-seed); regression — adapters unchanged when chunker off.

### 1.B — Active Memory pre-reply sub-agent

**The gap.** OC has reactive `RecallTool` + post-hoc `reviewer.py`. The pre-reply blocking gate pattern (sub-agent fires by default on every eligible reply, looks up memories, silently injects) is missing.

**Scope:**
- New extension `extensions/active-memory/` with `plugin.py` exposing `register(api: PluginAPI) -> PluginManifest` (Python-declarative — OC convention).
- Prompt templates at `extensions/active-memory/prompts/{balanced,strict,contextual,recall-heavy,precision-heavy,preference-only}.j2`.
- Reuses existing `HookEvent.PRE_LLM_CALL` (`plugin_sdk/hooks.py:57`) — no new hook event.
- **Additive SDK change:** add `mutable_messages: bool = False` field to `HookContext` so this plugin can mutate the live message list. Backward-compatible; default False preserves snapshot semantics for all existing hooks.
- Sub-agent has only `memory_search(query, limit)` and `memory_get(id)` tools, wired to existing `MemoryManager` (Honcho/Chroma/SQLite stack).
- Bounded by `timeoutMs` (default 8000ms) — fail-open if exceeded.
- Returns JSON `{"action": "inject"|"skip", "summary": "..."}`. On `inject`, prepends a fenced `<relevant-memories>` block as system message.
- Prompt-style tunables: `balanced` / `strict` / `contextual` / `recall-heavy` / `precision-heavy` / `preference-only`.
- Per-chat-type allowlist + `(chat_id, last_user_msg_hash)` cache within `cacheTtlMs`.
- Slash commands `/active-memory pause | resume | status`.
- Default: OFF (user opts in once trust is established).

**Tests:** prompt-style template rendering; cache key derivation; timeout fails-open; allowedChatTypes filter; PreLLMCall hook fires; inject/skip behavior; cache hit; snapshot semantics preserved for non-active-memory hooks.

### 1.C — Anti-loop / repetition detector

**The gap.** When the agent enters a degenerate tool-loop (same tool + same args repeatedly, or same assistant text turn after turn), nothing today catches it. Partial coverage via delegate `MAX_DEPTH` + `strict.yaml` loop budget; no per-call detector.

**Scope:**
- New `opencomputer/agent/loop_safety.py` with `LoopDetector(max_tool_repeats=3, max_text_repeats=2, window_size=10, max_consecutive_flags=2)`.
- Integrated into `agent/loop.py` after each tool call (`record_tool_call(name, args_hash)`) and after each assistant message (`record_assistant_text(text_hash)`).
- On `flagged()`: emit a structured warning into system reminders before next `provider.complete()`.
- On `must_stop()`: raise `LoopAbortError("agent loop aborted: repetition detected")` — caught by outer handler, surfaced to user.
- Default: ON, with permissive thresholds. Configurable via `agent.loop_safety` block in config.yaml.

**Tests:** detector flags 3rd identical tool-call; flags 2nd identical text; window-size correctly bounds memory; reset works between sessions; integration with synthetic 4-identical-Bash-call sequence; healthy sessions never trigger.

### 1.D — Replay sanitization

**The gap.** When OC catches up after offline gap (gateway restart, network blip, channel reconnect), it may re-process the assistant's stale buffered text or include it in subsequent prompts. OpenClaw has explicit strip-rules; OC `gateway/dispatch.py` doesn't.

**Scope:**
- New `opencomputer/gateway/replay_sanitizer.py` with `sanitize_for_replay(messages: list[Message]) -> list[Message]`:
  - Strip assistant turns that have a `replay=True` marker on the SessionDB row.
  - Strip outgoing-queue items already in-flight at gateway-restart time.
  - Drop user turns older than `replay_max_age_seconds` (default 300s) on cold start.
- Integration: `gateway/server.py::_replay_pending` calls `sanitize_for_replay(...)` before re-feeding to dispatch.
- Default: ON.

**Tests:** stale-marker strip; outgoing-queue dedup; max-age drop; integration with restart scenario.

### 1.E — Auth profile rotation cooldown + auto-monitor

**The gap.** OC's credential pool (`extensions/anthropic-provider/` + `openai-provider/`) rotates on hard 401/403 failures (Hermes H2 import). But there's no time-based cooldown for soft-failing profiles (e.g., one returns sporadic 5xx) and no periodic doctor monitor that proactively pings each profile.

**Scope:**
- New `plugin_sdk/credential_pool.py::CredentialPool.cooldown(profile_id, seconds)` — adds a profile to a temporary deny list with monotonic expiry.
- Modify provider plugins (`extensions/anthropic-provider/provider.py`, `extensions/openai-provider/provider.py`) to call `cooldown()` on transient errors (5xx, timeouts, connection-reset) with exponential backoff.
- New `opencomputer/doctor.py::auth_monitor_loop()` — runs every `auth_monitor_interval_seconds` (default 300s) in the background, pings each enabled profile with a tiny request (e.g., `models.list` or equivalent), demotes failing profiles via cooldown, restores recovered ones.
- Default: cooldown enabled; monitor loop OFF (opt-in via `~/.opencomputer/<profile>/config.yaml::auth.monitor.enabled: true`).

**Tests:** cooldown adds + expires correctly; transient error triggers cooldown; monitor loop demotes failing profile; recovered profile is restored; backoff doubles per failure.

### 1.F — Sessions-* tools

**The gap.** `/resume` slash and `oc session fork` CLI exist (G.33), but the *agent* can't programmatically spawn / send-to / list / inspect history / check status of parallel sessions. OpenClaw's inventory lists `sessions-spawn/send/list/history/status` as port targets.

**Scope (5 tools, 1 file):**
- New `opencomputer/tools/sessions.py` with 5 `BaseTool` subclasses:
  - `SessionsSpawn(name, prompt, model?)` — fork a new session from the current SessionDB; return new `session_id`.
  - `SessionsSend(session_id, message)` — enqueue a message to a sibling session via outgoing_queue cross-process channel.
  - `SessionsList()` — list all SessionDB sessions for the current profile (id, created_at, last_active, message_count, is_active).
  - `SessionsHistory(session_id, limit?)` — return recent N messages from the named session (read-only).
  - `SessionsStatus(session_id)` — return is_active, last_message_at, last_tool_used.
- All five register through the standard ToolRegistry; gated by F1 ConsentGate (capability claims: `sessions.spawn`, `sessions.send`, `sessions.list`, `sessions.history`, `sessions.status`).
- Default: tools registered; F1 prompts on first use.

**Tests:** spawn creates a SessionDB row + returns valid id; send writes to outgoing_queue + sibling reads; list returns expected rows; history returns slice; status returns flags; F1 ConsentGate blocks unauthorized use.

### 1.G — Clarify_tool

**The gap.** When the user's prompt is ambiguous, OC has `AskUserQuestion` (the agent must decide to call). OpenClaw has a `clarify_tool` that the agent can call to *auto-detect* ambiguity and offer concrete clarifying choices.

**Scope:**
- New `opencomputer/tools/clarify.py::ClarifyTool` — single tool with one schema:
  ```python
  schema = {
      "name": "Clarify",
      "description": "When the user's request is genuinely ambiguous (multiple plausible interpretations), call this with a list of concrete options. Do NOT call when the answer is obvious.",
      "input_schema": {
          "type": "object",
          "properties": {
              "ambiguity": {"type": "string", "description": "What's ambiguous about the request"},
              "options": {"type": "array", "items": {"type": "string"}, "description": "2-4 concrete interpretations"},
          },
          "required": ["ambiguity", "options"]
      }
  }
  ```
- Returns the selected option to the agent loop. Renders to the channel as a 2-4-button inline approval (Telegram: inline keyboard; Slack: Block Kit; Discord: select menu; TUI: numbered list with `AskUserQuestion`-style readline).
- Wraps existing `AskUserQuestion` machinery — the `ClarifyTool.run()` method just calls AUQ with a structured prompt.
- Default: tool registered; available in all sessions.

**Tests:** schema validates correctly; renders to TUI numbered list; renders to Telegram inline keyboard via channel handoff; agent receives selected option as the result.

### 1.H — Send_message_tool

**The gap.** Inventory lists as "port" target. OC has `outgoing_queue` infra (PR #221's cross-platform send path) but no agent-callable tool wrapper. Cross-channel workflows (e.g., "tell me on Slack when the Telegram review is done") aren't possible without it.

**Scope:**
- New `opencomputer/tools/send_message.py::SendMessageTool` — single tool:
  ```python
  schema = {
      "name": "SendMessage",
      "description": "Send a message to a specific channel (telegram/discord/slack/email/sms etc.) and peer. Use only when you need to deliver content to a destination DIFFERENT from the current conversation channel.",
      "input_schema": {
          "type": "object",
          "properties": {
              "channel": {"type": "string", "enum": [...all enabled channels...]},
              "peer": {"type": "string"},
              "message": {"type": "string"}
          },
          "required": ["channel", "peer", "message"]
      }
  }
  ```
- Routes through existing `PluginAPI.outgoing_queue.put_send(channel, peer, message)`.
- Gated by F1 ConsentGate (capability claim: `messaging.send.<channel>`).
- Default: tool registered; F1 prompts on first use per `<channel>`.

**Tests:** schema validates; routes to outgoing_queue with correct payload; rejects when channel not enabled; F1 prompts first-time.

## 5. Architecture interactions

```
                          ┌─────────────────────────┐
                          │  agent/loop.py          │
                          │  (per turn)             │
                          └──────────┬──────────────┘
                                     │
              ┌───────────[ 1.C anti-loop detector hook]
              │                      │
              │       ┌──────────────▼──────────────┐
              │       │ HookEngine.emit(            │
              │       │   PRE_LLM_CALL              │
              │       │   ctx.mutable_messages=True │  ← 1.B Active Memory hooks here
              │       │ )                           │
              │       └──────────────┬──────────────┘
              │                      │
              │              ┌───────▼────────┐
              │              │ provider call   │
              │              └───────┬────────┘
              │                      │
              │              ┌───────▼────────┐
              │              │ stream deltas   │
              │              └───────┬────────┘
              │                      │
              │       ┌──────────────▼──────────────┐
              │       │ dispatch.on_delta           │
              │       │  → adapter._send (1.A wrap) │  ← 1.A block chunker wraps here
              │       └──────────────┬──────────────┘
              │                      │
              │       ┌──────────────▼──────────────┐
              │       │ outgoing_queue              │  ← 1.H send_message_tool feeds here
              │       └──────────────┬──────────────┘
              │                      │
              │       ┌──────────────▼──────────────┐
              │       │ tool calls                  │
              │       │  → 1.F sessions-* tools     │
              │       │  → 1.G clarify_tool         │
              │       │  → 1.H send_message_tool    │
              │       └─────────────────────────────┘
              │
              └─[ 1.E auth cooldown + monitor — wraps provider client ]
              │
              └─[ 1.D replay sanitizer — runs at gateway cold start ]
```

## 6. Conflict map (parallel sessions)

| PR / session | Branch | Files touched | Overlap with this plan |
|---|---|---|---|
| archit-2 | `feat/runtime-pii-redaction` (#230) | `opencomputer/security/{__init__,redact}.py`, `tests/security/test_redact.py` | **None** |
| slash-queue | `feat/slash-queue-v2` (#231) | `/queue` slash | **None** |
| snapshot | `feat/slash-snapshot` (#232) | `cli.py`, `cli_ui/slash*.py`, NEW `opencomputer/snapshot/` | **None** |
| rollback | `feat/slash-rollback` (#233) | `coding-harness/plugin.py`, NEW `coding-harness/slash_commands/rollback.py` | **None** |
| Other 8 PRs (#220-#228) | Various | Various | Verified disjoint in earlier phases |
| /reload (paused) | `feat/slash-reload` | `cli.py` callbacks (paused mid-edit) | **None** |
| Hermes tail (not started) | `@filepath`, `--worktree`, Tirith, `/update`, `/restart`, `/debug` | TBD when started | **None expected.** |

**Coordination rule:** before adding the `mutable_messages` field to `HookContext` in 1.B, re-check whether any in-flight PR is mid-flight on `plugin_sdk/hooks.py` — if so, rebase 1.B's change after merge.

## 7. Defaults

| Pick | Default |
|---|---|
| 1.A block chunker | OFF for TUI; per-channel opt-in via config.yaml |
| 1.B Active Memory | OFF (must enable plugin via `oc plugin enable active-memory`) |
| 1.C anti-loop | ON, permissive thresholds |
| 1.D replay sanitization | ON |
| 1.E auth cooldown | ON; auto-monitor loop OFF (opt-in) |
| 1.F sessions-* tools | Registered; F1 prompts on first use |
| 1.G clarify_tool | Registered; available always |
| 1.H send_message_tool | Registered; F1 prompts on first use per channel |

## 8. Out of scope (explicit non-goals)

The following are explicitly **NOT** in this plan, with rationale recorded so we don't reopen them by accident:

- **Multi-agent isolation + binding router** — XL effort for speculative future-proofing; OC's single-user CLI doesn't need it.
- **Standing Orders DSL** — cron + scope grants cover ~95%; DSL is polish.
- **Hook taxonomy expansion** (4 marginal events) — no concrete need.
- **Inbound queue modes** (5 policies per channel) — OC `/queue` already in PR #231.
- **Cron session-isolation modes** — marginal polish.
- **Lobster typed workflow** — niche.
- **TaskFlow declarative pipelines** — scope-creep risk.
- **Background Tasks ledger expansion** — OC's `tasks/` sufficient.
- **Diagnostics OTEL** — no ops demand.
- **Heartbeat lane** — worsens RR-7 cron-consent risk.
- **OSC8 hyperlinks** — cosmetic.
- **Sandbox-browser variant** — no concrete demand.
- **memory-lancedb / memory-wiki** — Honcho+Chroma cover.
- **mcporter** — different vision.
- **Multi-stage approval rendering (web)** — Telegram+Slack already cover.
- **Skill Workshop auto-capture** — OC has auto-skill-evolution (PRs #193+#204).
- **50 provider plugins / 25 channel adapter long tail** — addressed by Hermes megamerge.
- **ACP bridge expansion** — gated on IDE adoption.
- **CLAUDE.md §5 Tier 5 wont-do** — canvas, mobile, voice-wake, Atropos RL, etc.

## 9. Acceptance

This plan is approved-pending-audit when:

1. All 8 picks have unit + integration tests; +0 regressions on existing suite.
2. ruff + mypy clean.
3. plugin_sdk boundary preserved (`tests/test_phase6a.py` still passes).
4. Manual smoke tests: chunker on Telegram (1.A); active-memory injects a relevant memory (1.B); synthetic loop aborts (1.C); replay drops stale text (1.D); cooldown demotes failing profile (1.E); sessions-spawn creates a row (1.F); clarify renders inline buttons on Telegram (1.G); send_message routes to other channel (1.H).
5. Each pick ships as one PR. Total ≤8 PRs.
