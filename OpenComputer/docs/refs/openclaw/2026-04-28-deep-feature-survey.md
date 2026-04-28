# OpenClaw Deep Feature Survey

**Source:** `/Users/saksham/Vscode/claude/sources/openclaw-2026.4.23/`
**Date:** 2026-04-28
**Purpose:** Comprehensive feature catalog for gap-audit against OpenComputer (Python agent framework).

---

## 1. TL;DR

- **OpenClaw is a "personal AI assistant that does things"** — a long-lived Gateway daemon owning *all* messaging surfaces (24+ channels, including WhatsApp/Baileys, Telegram, Slack, Discord, Signal, iMessage, Matrix, Twitch, Nostr) and routing inbound messages to one or more isolated agents. Not a coding harness, not a bot framework — a **single-user, multi-channel "command brain"**.
- **USP vs Hermes/Claude Code:** the channel inbox is the front door (not stdin/stdout), not just a side feature — Hermes was message-bot-first too, but OpenClaw shipped **mobile companion apps** (iOS/Android nodes) + **Live Canvas** + **Voice Wake / Talk Mode** + **Codex/ACP harness pluggability** that none of the others have. Claude Code is workspace-first; OpenClaw is **gateway-first**.
- **Architecture:** WebSocket-typed gateway protocol (TypeBox → JSON Schema → Swift codegen) with `connect` handshake, device pairing, idempotency keys, and per-session run queues. Plugin SDK is **manifest-first** (`openclaw.plugin.json` declares config schema, capabilities, contracts). 117 bundled extensions covering 50+ providers, 24+ channels, tooling, memory, and specialty subsystems.
- **Distinctive subsystems:** Active Memory (blocking pre-reply memory subagent), Dreaming (light/deep/REM phases + Dream Diary), SOUL.md persona, A2UI Live Canvas (websocket-driven HTML host), Codex app-server harness, ACP bridge for IDE editors, Skill Workshop (auto-skill capture), Standing Orders (autonomous program authority), Cron + Heartbeat (separate scheduling lanes), Background Tasks (activity ledger), Voice Wake (global wake word sync), Talk Mode (continuous voice loop), Voice Call (Telnyx/Twilio/Plivo SIP), Diagnostics-OTel (full OpenTelemetry).
- **Mobile:** Real iOS app (SwiftUI, Watch + Live Activity + Share Extension), Android (Kotlin/Jetpack Compose + benchmark module), macOS menu-bar + node mode + isolated MLX TTS helper. Not just a "send text" wrapper — these are full nodes that expose `canvas.*`, `camera.*`, `screen.record`, `location.get`, `device.*`, `notifications.*` commands the agent can invoke remotely.

---

## 2. Vision & Positioning (from VISION.md)

OpenClaw self-describes as **"the AI that actually does things. It runs on your devices, in your channels, with your rules."** Originally Warelay → Clawdbot → Moltbot → OpenClaw. Personal single-user assistant — **not** an agent-hierarchy framework. Vision explicitly rejects: full MCP-runtime-in-core (uses `mcporter` bridge), agent-hierarchy frameworks (manager-of-managers), wrapper channels, heavy orchestration layers.

Stated priorities: security/safe defaults → bug-fix/stability → setup reliability → all major model providers → all major messaging channels → performance/test infra → computer-use/agent harness → CLI+web frontend ergonomics → companion apps. **Plugin path is npm package + local extension loading; bar for adding to core is intentionally high — most new skills go to ClawHub** (`clawhub.ai`), not bundled.

Why TypeScript: "OpenClaw is primarily an orchestration system: prompts, tools, protocols, and integrations. TS keeps it hackable." — maps directly to OpenComputer's Python orchestration layer.

---

## 3. Architecture Overview

### 3.1 Gateway (the daemon)

- Single long-lived process at `127.0.0.1:18789` by default.
- Owns **all** channel connections (one Baileys session per host, etc.).
- Exposes typed WebSocket API + HTTP routes (Canvas at `/__openclaw__/canvas/`, A2UI at `/__openclaw__/a2ui/`).
- Validates inbound frames against JSON Schema (TypeBox-generated).
- Emits typed events: `agent`, `chat`, `presence`, `health`, `heartbeat`, `cron`, `voicewake.changed`, etc.
- File: `src/gateway/` (~150 files), `docs/gateway/` for protocol/auth/sandboxing/tailscale/discovery/health/heartbeat.
- Auth modes: `shared-secret`, `trusted-proxy`, `tailscale-allow`, `none` (private ingress only).
- Process supervision via launchd/systemd.

### 3.2 Wire protocol

- Transport: WebSocket text frames, JSON.
- First frame **must** be `connect` with device identity + capability declaration. Pairing approval issues a device token.
- Requests: `{type:"req", id, method, params}` → `{type:"res", id, ok, payload|error}`.
- Events: `{type:"event", event, payload, seq?, stateVersion?}`.
- Idempotency keys required for `send`/`agent` (short-lived dedupe cache).
- Devices declare `role: "node"` + caps + commands + permissions.
- Schema generated from TypeBox; Swift models codegen'd from JSON Schema for iOS/macOS apps.
- `docs/concepts/architecture.md`, `docs/gateway/protocol.md`.

### 3.3 Plugin SDK (`packages/plugin-sdk/`, `src/plugin-sdk/`)

- **Manifest-first.** Every plugin has `openclaw.plugin.json` declaring `id`, `configSchema` (JSON Schema), `uiHints`, `contracts`, `commandAliases`, `activation`, etc.
- Plugins import only from narrow subpaths: `openclaw/plugin-sdk/plugin-entry`, `.../channel-core`, `.../config-runtime`, `.../agent-runtime`, etc. (200+ subpaths in `scripts/lib/plugin-sdk-entrypoints.json`).
- `definePluginEntry({ id, register(api) })` is the entry pattern. `api.register*(...)` covers: Provider, AgentHarness, CliBackend, Channel, SpeechProvider, RealtimeTranscriptionProvider, RealtimeVoiceProvider, MediaUnderstandingProvider, ImageGenerationProvider, MusicGenerationProvider, VideoGenerationProvider, WebFetchProvider, WebSearchProvider, Tool, Command, Hook, HttpRoute, GatewayMethod, Cli, Service, InteractiveHandler, EmbeddedExtensionFactory, MemoryPromptSupplement, MemoryCorpusSupplement.
- Exclusive slots (one active at a time): `ContextEngine`, `MemoryCapability`, `MemoryPromptSection`, `MemoryFlushPlan`, `MemoryRuntime`.
- Hook events list: `before_model_resolve`, `before_prompt_build`, `before_agent_start`, `before_agent_reply`, `agent_end`, `before_compaction`/`after_compaction`, `before_tool_call`/`after_tool_call`, `before_install`, `tool_result_persist`, `message_received`/`message_sending`/`message_sent`, `session_start`/`session_end`, `gateway_start`/`gateway_stop`. Decision rules for each (block/cancel terminal vs no-op).
- `docs/plugins/sdk-overview.md`, `docs/plugins/architecture.md`.

### 3.4 Tool dispatch / approval flow

- `src/agents/bash-tools.exec-*` family — separate runtimes for foreground/background, host-gateway vs host-node, PTY fallbacks, approval-id propagation.
- `docs/tools/exec-approvals.md`, `docs/tools/exec-approvals-advanced.md`, `docs/tools/elevated.md`.
- Approval surface flows: native (in-process), client-helpers (UI/web), delivery (channel render), reply-runtime (chat ack). See `src/plugin-sdk/approval-*.ts` (15+ files).
- Sandbox tiers via `sandbox.mode: "non-main"|"all"|"never"`, backend `docker|ssh|openshell`. Default policy: allow `bash/process/read/write/edit/sessions_*`; deny `browser/canvas/nodes/cron/discord/gateway`. `docs/gateway/sandboxing.md`, `docs/gateway/sandbox-vs-tool-policy-vs-elevated.md`.

### 3.5 Memory architecture

- Two-tier model: **active** (one capability slot at a time via `registerMemoryCapability`) + **prompt supplements / corpus supplements** (additive).
- Backends: `memory-core` (built-in MEMORY.md / DREAMS.md), `memory-lancedb` (vector DB w/ OpenAI embeddings), `memory-wiki` (Obsidian-style vault, isolated/bridge/unsafe-local modes), `memory-honcho` (third-party), `memory-qmd` (markdown corpus search).
- **Active Memory plugin** (`extensions/active-memory/`): bounded blocking sub-agent that runs *before* the main reply, calling `memory_search`/`memory_get` via a recall model (cerebras/gpt-oss-120b is recommended fast option). Injected as hidden untrusted prompt prefix. Uses prompt styles: `balanced`/`strict`/`contextual`/`recall-heavy`/`precision-heavy`/`preference-only`. `docs/concepts/active-memory.md`.
- **Dreaming**: 3-phase background consolidation (Light: ingest + dedupe; Deep: rank + promote → MEMORY.md; REM: reflective patterns). Weighted scoring (frequency, relevance, query-diversity, recency, consolidation, conceptual-richness). Runs as an isolated cron lane. Writes machine state to `memory/.dreams/`, human-readable Dream Diary to `DREAMS.md` + per-phase reports under `memory/dreaming/<phase>/`. `docs/concepts/dreaming.md`.

### 3.6 Streaming (RxJS-not-quite)

- Block streaming: assistant deltas → chunker → block-level channel sends (text-end vs message-end break), idle-coalescing, paragraph/newline/sentence preference, never split inside code fences.
- Preview streaming: temp message that gets edited (Telegram/Slack/Discord).
- No raw token-delta streaming to channels.
- `EmbeddedBlockChunker` does the work (`src/...blockChunker`); `humanDelay` config for natural pacing between blocks (`natural`: 800-2500ms randomized, `custom: minMs/maxMs`).
- Lifecycle/assistant/tool/compaction streams emitted to RPC callers.
- `docs/concepts/streaming.md`.

---

## 4. Extension Catalog (117 extensions)

### 4.1 Provider plugins (text inference) — 50

| Plugin | Notes |
|---|---|
| `anthropic` | Claude — replay-policy + transport-stream + setup-token + payload-log + payload-policy |
| `anthropic-vertex` | GCP-hosted Anthropic with ADC discovery |
| `openai` | OpenAI + Codex OAuth + `gpt-image-2` reference editing |
| `openai` (subscription) | OAuth path tied to ChatGPT/Codex |
| `google` | Gemini + Vertex + image/embedding/media-understanding |
| `groq` | OpenAI-compat |
| `mistral`, `deepseek`, `moonshot`, `kimi-coding`, `kilocode` | OpenAI-compat |
| `ollama`, `lmstudio`, `vllm`, `sglang`, `litellm`, `vercel-ai-gateway`, `cloudflare-ai-gateway` | Local + proxy backends |
| `openrouter` | Multi-model proxy with OAuth |
| `xai` | Grok + native image-gen + STT + realtime |
| `huggingface` | HF inference |
| `together`, `fireworks`, `chutes`, `nvidia`, `cerebras` (impl via openai-compat) | Cloud inference |
| `microsoft`, `microsoft-foundry`, `amazon-bedrock`, `amazon-bedrock-mantle` | Enterprise clouds |
| `voyage` | Voyage embeddings |
| `arcee`, `synthetic`, `tokenjuice`, `venice`, `vydra` | Niche/long-tail |
| `alibaba`, `byteplus`, `qianfan`, `qwen`, `zai`, `tencent`, `volcengine`, `xiaomi`, `stepfun`, `minimax` | China-region providers (large set) |
| `perplexity` | Perplexity + web-search |
| `codex` | **OpenAI Codex app-server** harness — websocket transport, guardian/yolo modes, native media-understanding (gpt-5.5 image) |
| `acpx` | **Embedded ACP runtime** (codex CLI / opencode / claude CLI / gemini CLI) — yields tools to external CLI agents |
| `copilot-proxy` | GitHub Copilot via local proxy |
| `github-copilot` | First-party Copilot |
| `opencode`, `opencode-go` | OpenCode CLI agent harnesses |

### 4.2 Channel adapters — 25

| Channel | Notes |
|---|---|
| `discord` | Multi-account, slash commands, native role gating, MPIM group classification |
| `telegram` | Multi-account, forum topics support, MarkdownV2 formatter, image inline reply |
| `whatsapp` | Baileys-based, single-session-per-host invariant, contact/vCard untrusted-metadata fenced |
| `slack` | Workspaces, MPIM group DMs, thread bindings |
| `signal` | signal-cli backend |
| `imessage` | Legacy macOS imessage |
| `bluebubbles` | iMessage proxy via BlueBubbles server |
| `matrix` | E2EE notes (Track B) — verification, push rules |
| `mattermost`, `nextcloud-talk`, `tlon`, `synology-chat` | Self-hosted enterprise chat |
| `feishu`, `line`, `wechat`/`zalo`/`zalouser`, `qqbot` | China/APAC channels |
| `googlechat`, `msteams` | Google Workspace + Microsoft 365 |
| `irc`, `nostr`, `twitch` | Open / decentralized channels |
| `webhooks` | Generic HTTP webhook channel |
| `qa-channel` | Internal QA test channel substrate |
| `voice-call` | **Telnyx + Twilio + Plivo SIP** with webhook + ngrok/tailscale tunnel + realtime STT/TTS streaming |
| `phone-control` | Phone control (paired with voice-call) |
| `device-pair` | `/pair` command + QR image generation for device pairing |

### 4.3 Tool / capability plugins — 13

| Plugin | Capability |
|---|---|
| `browser` | Chromium via CDP — bridge, profiles, cookies, doctor, host-inspection, control-auth |
| `comfy` | ComfyUI workflows — image + music + video generation |
| `fal` | Fal.ai providers — image + video |
| `runway` | Runway video-gen |
| `elevenlabs` | TTS + STT + media-understanding + realtime-transcription |
| `deepgram` | STT (realtime + batch) |
| `exa`, `firecrawl`, `tavily`, `brave`, `duckduckgo`, `searxng` | Web-search providers |
| `image-generation-core`, `video-generation-core`, `media-understanding-core`, `speech-core` | Capability cores (provider-agnostic surfaces) |

### 4.4 Memory & Knowledge — 4

| Plugin | Description |
|---|---|
| `memory-core` | MEMORY.md + DREAMS.md (default) — owns `dreaming` slash command, manages dreaming cron |
| `memory-lancedb` | LanceDB vector store — OpenAI embeddings, auto-capture, auto-recall |
| `memory-wiki` | Obsidian-friendly wiki vault — isolated/bridge/unsafe-local modes, `useOfficialCli` for Obsidian binary |
| `active-memory` | Pre-reply blocking memory sub-agent (described in 3.5) |

### 4.5 Specialty / infra plugins — ~8

| Plugin | What it does |
|---|---|
| `acpx` | Agent Client Protocol runtime — bridges Codex/Gemini/Claude CLIs as OpenClaw agents. Owns its own session/transport. `permissionMode`: approve-all/approve-reads/deny-all. MCP server injection. |
| `codex` | Native Codex app-server — separate from acpx; designed for the Codex-managed gpt-5.5 catalog with OpenAI OAuth |
| `copilot-proxy` | Provider proxy for Copilot (in-process) |
| `diagnostics-otel` | Full OpenTelemetry — OTLP traces/metrics/logs exporters, redacted attributes, batch processors |
| `device-pair` | `/pair` command, QR setup codes for mobile |
| `skill-workshop` | **Auto-captures repeatable workflows as workspace skills**. Heuristic + LLM-judge review, pending review queue, max-pending limits, max-bytes per skill. (Direct equivalent of OpenComputer's auto-skill-evolution.) |
| `lobster` | **Typed workflow tool with resumable approvals** — workflow nodes that pause for human approval and resume |
| `llm-task` | LLM-as-a-task primitive |
| `talk-voice` | Voice-selection slash commands (`/voice list`, `/voice set`) |
| `voice-call` | SIP voice calling (described above) |
| `phone-control` | Phone-call orchestration tool |
| `thread-ownership` | Per-thread agent assignment |
| `diffs` | Read-only diff viewer + file renderer w/ HTML viewer base URL, PNG/PDF rendering |
| `qa-lab`, `qa-matrix`, `qa-channel` | Internal QA harnesses (matrix homeserver lane, etc.) |
| `openshell` | OpenShell sandbox backend |
| `open-prose` | Prose tool |
| `test-support`, `shared` | Test infra |

---

## 5. Distinctive Features (the ones NOT in Hermes/Claude Code)

- **A2UI Live Canvas** (`src/canvas-host/`, `src/canvas-host/a2ui/a2ui.bundle.js`) — a websocket-driven HTML/CSS/JS host. The agent edits files in `/__openclaw__/canvas/`; the canvas page auto-reloads via chokidar+ws. Mobile/macOS apps can render the canvas. Companion `dream-diary-preview-v2.html` and `v3.html` are full standalone preview pages of the Dream UI.
- **Voice Wake (global wake word)** — Gateway owns the wake-word list at `~/.openclaw/settings/voicewake.json`. Edit from any node; broadcast `voicewake.changed` event to all clients/nodes. Supports macOS + iOS wake; Android falls back to manual mic. Defaults: `["openclaw", "claude", "computer"]`. `docs/nodes/voicewake.md`.
- **Talk Mode** — continuous voice loop: listen → STT → model → speak. `Listening|Thinking|Speaking` overlay, `interruptOnSpeech`, voice JSON directives in reply (`{"voice":"X","once":true}`), per-platform output formats (PCM 24kHz on Android, etc.). `docs/nodes/talk.md`.
- **Voice Call (SIP)** — accepts inbound phone calls via Telnyx/Twilio/Plivo; closed-loop realtime audio bridge; ngrok/tailscale tunnel; allowlist; `responseModel`/`responseSystemPrompt` overrides.
- **ACP bridge** (`src/acp/`, `docs.acp.md`) — exposes OpenClaw as ACP agent over stdio for Zed/IDEs. Runtime + control-plane manager. `loadSession` replays history; per-session MCP servers rejected. Tool streaming partial.
- **Codex app-server harness** (`extensions/codex/src/app-server/`) — full Codex CLI + websocket-mode integration. Guardian/yolo modes, sandbox tiers (read-only/workspace-write/danger-full-access). Native image-gen via Codex OAuth.
- **Standing Orders** (`docs/automation/standing-orders.md`) — text contract in `AGENTS.md` granting agent permanent operating authority for "programs" (scope, triggers, approval gates, escalation rules). Combined with cron for time-based enforcement.
- **Skill Workshop** (`extensions/skill-workshop/`) — auto-capture of reusable workflows as skills. Heuristic + LLM-judge review. Pending → applied/quarantined. Maps to OpenComputer's auto-skill-evolution (already shipped).
- **Lobster** (`extensions/lobster/`) — typed workflow tool with resumable approvals. Long-running workflows that pause + resume across sessions.
- **Background Tasks** (`src/tasks/`, `docs/automation/tasks.md`) — separate "activity ledger" tracking ACP/subagent/cron/CLI runs. State machine: `queued → running → succeeded|failed|timed_out|cancelled|lost`. Push-driven completion (no polling). 7-day retention.
- **Heartbeat** vs **Cron** — two distinct background-execution lanes. Heartbeat = always-on agent tick; Cron = scheduled jobs. Dreaming runs as isolated cron (decoupled from heartbeat after recent fix). `docs/automation/cron-vs-heartbeat.md`.
- **TaskFlow** (formerly ClawFlow, `docs/automation/taskflow.md`) — declarative task pipelines.
- **Standing Orders + Cron + TaskFlow + Heartbeat** = full autonomous-program subsystem.
- **Multi-agent isolation** (`docs/concepts/multi-agent.md`) — *truly* isolated brains: workspace + agentDir + auth-profiles + session store all per-agent. Bindings route inbound by `(channel, accountId, peer, parentPeer, guildId, roles, teamId)`. Most-specific match wins; deterministic. **Sounds heavy; OpenComputer's persona-router is comparable but lighter.**
- **Delegate architecture** (`docs/concepts/delegate-architecture.md`) — extends multi-agent to organizational delegation (Tier 1 read-only/draft; Tier 2 send-on-behalf; Tier 3 proactive autonomous). Hard blocks in SOUL.md/AGENTS.md, tool deny lists, sandbox-all, audit trail.
- **Diagnostics OpenTelemetry** (`extensions/diagnostics-otel/`) — full OTLP exporter (traces/metrics/logs), batch processors, sample-rate, attribute redaction. Plug-in service that subscribes to gateway events.
- **`mcporter` MCP bridge** — MCP servers configured outside core (decoupled). Reload without restarting gateway. Vision explicitly rejects first-class MCP runtime in core.
- **Device pairing v3** (`extensions/device-pair/`) — QR images for pairing, signature payload `v3` binds platform+deviceFamily, requires repair-pairing on metadata change. Tailnet binds still require pairing.
- **Live config reload + secrets reload** — `openclaw secrets reload` invalidates webhook SecretRefs immediately.
- **Auth profiles** (`src/agents/auth-profiles*`) — per-agent rotation, cooldown auto-expiry, doctor checks, last-used ordering, external CLI sync. `~/.openclaw/agents/<id>/agent/auth-profiles.json`. `docs/concepts/model-failover.md`.
- **Action handler-adapter runtime** + 15+ approval-* files — multi-surface approval rendering (native UI, client-helpers UI, delivery channel, reply runtime).
- **Inbound queue modes** — `steer`/`followup`/`collect`/`steer-backlog`/`interrupt`. Per-session and per-channel. Debounce + cap + drop policy (`old`/`new`/`summarize`). `docs/concepts/queue.md`.
- **Replay sanitization for assistant text** — strips thinking tags, `<relevant-memories>`, plain-text tool-call XML, downgraded scaffolding, leaked control tokens (`<|assistant|>`), full-width tokens (`<｜...｜>`), malformed MiniMax invokes — before redaction/truncation.
- **Internal hooks** — `session-memory`, `bootstrap-extra-files`, `command-logger`, `boot-md` bundled hooks. Hook-pack discovery via npm packages.
- **Anti-loop / loop detection** — `docs/tools/loop-detection.md` (referenced; not opened).
- **Trajectory** (`src/trajectory/`) — plan/trajectory storage and replay.
- **Polls** (`src/polls.ts`) — poll primitives.
- **Auth monitoring** (`docs/automation/auth-monitoring.md`) — automated provider-auth health checks.
- **Gmail Pub/Sub trigger** (`docs/automation/gmail-pubsub.md`) — Gmail push-message → cron run with model override.

---

## 6. Mobile Story

OpenClaw mobile is **not just a chat app** — apps are **nodes** that expose command surfaces.

### 6.1 macOS (`apps/macos/`)
- SwiftUI menu-bar app
- `OpenClaw`, `OpenClawDiscovery`, `OpenClawIPC`, `OpenClawMacCLI`, `OpenClawProtocol` Swift packages
- WebChat embed + debug tools
- Voice Wake + push-to-talk overlay
- Remote SSH gateway control
- Connects in **node mode** to expose its local canvas/camera commands to the agent
- macOS code signing required for permissions to stick (`docs/platforms/mac/permissions.md`)

### 6.2 macOS MLX TTS (`apps/macos-mlx-tts/`)
- **Isolated** Swift package using `mlx-audio-swift` 0.1.2
- Out-of-process TTS helper (kept off the macOS app's main Package.swift to avoid bloating tests)
- Bridges to OpenClaw via TTS provider contract

### 6.3 iOS (`apps/ios/Sources/`)
- 17 modules: `Calendar`, `Camera`, `Capabilities`, `Chat`, `Contacts`, `Device`, `EventKit`, `Gateway`, `LiveActivity`, `Location`, `Media`, `Model`, `Motion`, `Onboarding`, `Push`, `Reminders`, `Screen`, `Services`, `Settings`, `Status`, `Voice`
- **WatchApp** + **WatchExtension** (Apple Watch support)
- **ShareExtension** (system share sheet)
- **ActivityWidget** (Live Activities/Dynamic Island)
- Fastlane for CI builds
- Pairs over WS as `role: node` with caps; agent invokes commands like `camera.takePhoto`, `screen.record`, `location.get`, `notifications.send`

### 6.4 Android (`apps/android/`)
- Kotlin + Jetpack Compose (Gradle KTS)
- `app/`, `benchmark/` (Macrobenchmark), `play/`, `test/`, `main/` source sets
- Voice tab with manual mic flow (Voice Wake disabled on Android currently)
- Same node-mode pattern, exposes device commands
- Recent security tightening: loopback-only cleartext WS, no LAN ws://

### 6.5 Apps shared (`apps/shared/OpenClawKit`)
- Cross-app Swift SDK

### 6.6 Mobile-only features
- iOS/Android **node pairing** (separate from device-pair signing)
- Push notifications (iOS)
- Camera/photo capture with capability declaration
- Screen recording (iOS)
- Location capture (one-shot + continuous)
- Calendar/Reminders/Contacts integration (iOS-only via EventKit)
- Live Activity for in-progress agent runs
- Watch app (extremely thin client)
- Share Extension (send text/image into agent)

---

## 7. Specialty Subsystems

### 7.1 Sandbox (Docker tiers)
- `Dockerfile.sandbox`: debian:bookworm-slim + bash/curl/git/jq/python3/ripgrep, runs as `sandbox` user
- `Dockerfile.sandbox-browser`: same base + Chromium + xvfb + x11vnc + novnc + websockify + socat (full headless browser with VNC bridge on ports 9222/5900/6080)
- `Dockerfile.sandbox-common`: shared layer
- Cache mounts for apt
- `docs/gateway/sandboxing.md`, `docs/gateway/sandbox-vs-tool-policy-vs-elevated.md`

### 7.2 ACP (Agent Client Protocol)
- `src/acp/` (~40 files) — runtime, control-plane, server, translator, persistent-bindings, secret-file
- `docs.acp.md` (root) — bridge scope, compatibility matrix, Zed setup
- Implementation status: core flow, listSessions, slash commands; partial: loadSession, prompt content, session modes, tool streaming; unsupported: per-session MCP, fs/* methods, terminal/* methods, plans/thoughts

### 7.3 Live Canvas + A2UI
- `src/canvas-host/server.ts` — HTTP + WS server (chokidar live reload)
- `src/canvas-host/a2ui/index.html` + `a2ui.bundle.js` — A2UI runtime
- Canvas hash `src/canvas-host/a2ui/.bundle.hash` (generated)
- `docs/platforms/mac/canvas.md`
- Agent edits files; canvas reloads automatically via WS notification

### 7.4 Codex / app-server
- `extensions/codex/src/app-server/` — full Codex protocol (request_user_input, command approvals, login refresh)
- `extensions/codex/src/commands.ts` — slash commands routed to Codex CLI
- Native gpt-5.5 image gen via OAuth-only path
- Embedded extension factory (`contracts.embeddedExtensionFactories: ["pi"]`)

### 7.5 Pi-agent-core (the inner loop)
- Core inference loop = `pi-agent-core` (referenced as upstream/bundled lib); OpenClaw wraps it with session/discovery/tool-wiring/channel-delivery
- `runEmbeddedPiAgent` is the entry; `subscribeEmbeddedPiSession` bridges events to OpenClaw stream.

### 7.6 Sessions / sub-agents (`src/sessions/`)
- `sessions_list`/`sessions_history`/`sessions_send`/`sessions_spawn`/`sessions_yield`/`subagents`/`session_status`
- `sessions_spawn` supports `runtime: subagent|acp`, `context: isolated|fork`, `thread: true` for chat-thread binding, `sandbox: require`
- `subagents` orchestrator helper — list/steer/kill children
- Default leaf sub-agents have **no** session tools; depth-1 orchestrators get them

### 7.7 Cron + Heartbeat + TaskFlow + Tasks
- `src/cron/` (~50 files) — service, isolated-agent runs, normalize, schedule (croner), session-reaper, run-log, stagger, heartbeat-policy
- `src/tasks/` — task-registry SQLite store, taskflow registry, executor policy, owner-access, status, audit, maintenance
- Cron job formats: `at` (one-shot), `every` (interval), `cron` (5/6-field with TZ + stagger)
- Cron sessions: `main|isolated|current|session:<id>`
- Webhook delivery, announce delivery, none mode
- Failure destination override, separate from primary delivery

### 7.8 Realtime voice + transcription (`src/realtime-voice/`, `src/realtime-transcription/`)
- Provider-registry + provider-resolver + websocket-session
- Plug-in providers: ElevenLabs, Deepgram, OpenAI, xAI, Mistral

### 7.9 Diagnostics OTEL
- Full OTLP-proto exporters (traces/metrics/logs), batch processors, parent-based sampler with TraceIdRatio, attribute redaction
- Subscribes to plugin-broadcast `DiagnosticEventPayload` via `onDiagnosticEvent`

### 7.10 TUI (`src/tui/`)
- Local terminal chat with embedded backend
- Local-shell bridging
- OSC8 hyperlinks
- Stream assembler with overlays
- Submit/handlers/formatters/waiting indicator
- Separate from the gateway WS path

### 7.11 Wizard / onboard (`src/wizard/`)
- @clack/prompts-based interactive setup
- Plugin config + secret input + security-note + completion + finalize stages
- `openclaw onboard` is the recommended setup path
- Auto-installs missing provider/channel plugins during setup (recent change)

---

## 8. What Seems Most Port-Worthy to Python (OpenComputer)

### High value — port the API shape, not the impl

1. **Manifest-first plugin model** — `openclaw.plugin.json`-style schema with `id`, `configSchema` (JSON Schema), `uiHints`, `contracts`, `commandAliases`, `activation`. Python could use Pydantic for schema. OpenComputer's plugin-sdk is already capability-claims based; this would make config + UI generation auto. (`extensions/*/openclaw.plugin.json`)
2. **Hook taxonomy** — the precise set of `before_model_resolve | before_prompt_build | before_agent_start | before_agent_reply | agent_end | before/after_compaction | before/after_tool_call | before_install | tool_result_persist | message_received|sending|sent | session_start|end | gateway_start|stop`. Block/cancel decision rules are precise. Direct port to Python event-emitter. (`docs/concepts/agent-loop.md`, `docs/plugins/sdk-overview.md`)
3. **Active Memory plugin pattern** — bounded blocking pre-reply sub-agent with `memory_search`/`memory_get` only. Hidden untrusted prompt prefix. Eligibility gates (chat-type + agent + plugin). Slash-command session toggle. (`extensions/active-memory/`)
4. **Dreaming / Dream Diary** — three-phase consolidation (light/deep/REM), weighted ranking, optional REM diary subagent, grounded historical backfill lane. Maps onto OpenComputer's Layer 3 deepening. (`docs/concepts/dreaming.md`, `extensions/memory-core/`)
5. **Standing Orders + Cron + Tasks** — declarative `## Program: X` contracts in AGENTS.md combined with cron jobs + tasks ledger. Direct Python port. (`docs/automation/standing-orders.md`, `docs/automation/cron-jobs.md`, `docs/automation/tasks.md`)
6. **Skill Workshop auto-capture** — heuristic + LLM-judge dual-stage filter for capturing skills from successful runs. Pending review queue, max-bytes. **OpenComputer's auto-skill-evolution already covers this** but the Workshop's pending/quarantined/applied state machine + max-pending limits + LLM judge are sharper. (`extensions/skill-workshop/`)
7. **Lobster typed workflow tool with resumable approvals** — tasks that pause for approval and resume. Excellent fit for OpenComputer's exec-approval surface. (`extensions/lobster/`)
8. **Multi-agent routing** — full `agentId` × `accountId` × `peer` × `parentPeer` × `guildId` × `teamId` deterministic most-specific-wins binding. Already partially in OpenComputer; OpenClaw's `bindings:` config shape is clean. (`docs/concepts/multi-agent.md`)
9. **Background Tasks ledger** — separate from sessions, push-driven completion, 7-day retention, audit + maintenance commands. (`docs/automation/tasks.md`, `src/tasks/`)
10. **Voice Wake (global wake-word sync)** — single Gateway-owned list; broadcast to all clients. OpenComputer voice-mode is on-demand; this is the always-on path. (`docs/nodes/voicewake.md`)
11. **Talk Mode loop semantics** — interrupt-on-speech, voice JSON directives, silence-timeout, listening/thinking/speaking phases. (`docs/nodes/talk.md`)
12. **Diagnostics OTEL plugin pattern** — pluggable observability via OTLP. Direct port using `opentelemetry-api`/`opentelemetry-sdk` Python. (`extensions/diagnostics-otel/`)
13. **Streaming chunker (block streaming)** — chunker with min/max bounds, paragraph/newline/sentence/whitespace preference, never-split-fences, idle coalescing, randomized human-pacing delay. Algorithm portable. (`docs/concepts/streaming.md`)
14. **Session queue modes** — `steer`/`followup`/`collect`/`steer-backlog`/`interrupt` per-channel + per-session. Direct port. (`docs/concepts/queue.md`)
15. **Replay sanitization for assistant text** — the precise set of strip-rules (thinking tags, `<relevant-memories>`, tool-call XML, control tokens, full-width tokens). Critical for cross-provider compat. (`docs/concepts/multi-agent.md` § sessions_history)
16. **ACP bridge** — exposing OpenComputer as an ACP agent over stdio for Zed/IDE integration. Worth doing if any IDE adoption. (`docs.acp.md`, `src/acp/`)
17. **Sandbox tiers via Docker** — the precise default allow/deny tool list (`bash/process/read/write/edit/sessions_*` allow; `browser/canvas/nodes/cron` deny). (`README.md` § Security model, `docs/gateway/sandboxing.md`)
18. **Approval-runtime split** — native vs delivery vs reply renderers (15+ files). For OpenComputer, the *idea* of multiple approval surfaces (terminal vs web vs channel ack) is what matters. (`src/plugin-sdk/approval-*.ts`)

### Medium value — concept transfers but needs adaptation

19. **Live Canvas (A2UI)** — the websocket-driven HTML host. OpenComputer is CLI-first; could be a stretch goal for a future GUI surface.
20. **Codex / ACP harness pluggability** — the idea of plugging in external CLIs (codex, opencode, claude) as agent harnesses. OpenComputer's harness layer could absorb this.
21. **Delegate architecture / hard blocks** — the Tier-1/2/3 framework + SOUL/AGENTS hard-block discipline. Direct port to a deployment guide.

### Specific feature names worth grepping

- `humanDelay` (random pause between blocks)
- `blockStreamingCoalesce` (idle merge)
- `chunkMode: "newline"` (paragraph-boundary-first chunking)
- `messages.queue.byChannel` (per-channel queue mode)
- `agents.defaults.heartbeat` (agent-scoped heartbeat config)
- `tools.experimental.planTool` (`update_plan` tool)
- `tools.media.asyncCompletion.directSend` (direct channel delivery for async media gen)

---

## 9. What Seems NOT Port-Worthy

### Tied to TS / NestJS / RxJS / Node ecosystem
- **TypeBox → JSON Schema → Swift codegen pipeline** — Python has Pydantic + apischema; codegen path differs. Replicate the *invariant* (typed protocol contract that mobile apps consume), not the toolchain.
- **`tsdown`/`tsgo` build pipeline** — irrelevant to Python.
- **`pnpm` workspace + 117-package monorepo layout** — Python uses `uv`/`hatch`/poetry; map the *concept* (one repo, plugins as packages) but not the manager.
- **chokidar file-watching for canvas live-reload** — Python has watchfiles; works the same but file-watching isn't really a feature gap.
- **Baileys WhatsApp library** — Python has `whatsapp-cloud-api`/`yowsup` etc., totally different. Channel adapter would be a from-scratch port.
- **grammY for Telegram, Carbon for Discord** — Python has `python-telegram-bot`, `discord.py`. Direct equivalents.
- **Vitest + per-shard timing artifact** — Python has pytest-xdist; concept transfers, file format doesn't.
- **`@buape/carbon` Discord lib (owner-pinned)** — irrelevant.

### Truly mobile-native
- **iOS Swift code (WatchKit, EventKit, ActivityWidget)** — port-worthy only if OpenComputer ships its own iOS app.
- **Android Kotlin + Jetpack Compose** — same.
- **Apple Watch app** — same.
- **macOS menu-bar app + Sparkle appcast** — same.
- **macOS MLX TTS (mlx-audio-swift)** — Apple-Silicon-native; Python alternative is `mlx-lm` for inference, but TTS-via-MLX is a Mac-only path.

### Low value / single-purpose

- **117 specific channel adapters** — OpenComputer should pick a Tier-1/2/3 priority, not port everything. Already shipped: matrix, mattermost, signal, whatsapp, email, webhook, slack, homeassistant, sms (per memory). Channels OpenClaw has that OpenComputer doesn't and probably shouldn't bother: nostr, twitch, irc, qqbot, zalo/zalouser/wechat, line, feishu, tlon, synology-chat, nextcloud-talk, googlechat, msteams.
- **50+ provider plugins** — Python's `litellm`/`aisuite` already covers most. Don't re-implement Volcengine/QianFan/etc. unless there's user demand.
- **Voice-call SIP integration (Telnyx/Twilio/Plivo)** — substantial infra (webhooks, ngrok, signature verification per provider, realtime audio streaming). Skip unless phone calling is on the roadmap.
- **macOS-specific permissions handling** — irrelevant to Linux/Windows OpenComputer.
- **Bonjour/mDNS discovery** (`docs/gateway/bonjour.md`) — Python has zeroconf; works but not a feature gap.

### Replicates existing OpenComputer infra
- **`mcporter` MCP bridge** — OpenComputer already has MCP integration via Claude Agent SDK. Skip.
- **Sparkle update appcast** — OpenComputer's update path is `pip install -U`.
- **Carbon/Trello/Spotify/Sonos/etc. bundled skills** — these belong in user workspaces, not core. OpenComputer already ships skills selectively.

---

## 10. File Pointers (cheat sheet)

| Subsystem | Path |
|---|---|
| Vision | `VISION.md`, `README.md`, `CLAUDE.md`, `AGENTS.md` |
| Core gateway | `src/gateway/` (~150 files), `docs/gateway/` |
| Plugin SDK | `packages/plugin-sdk/`, `src/plugin-sdk/`, `docs/plugins/sdk-overview.md` |
| Manifest format | `extensions/*/openclaw.plugin.json` (any) |
| Active memory | `extensions/active-memory/`, `docs/concepts/active-memory.md` |
| Dreaming | `extensions/memory-core/`, `docs/concepts/dreaming.md`, `dream-diary-preview-v2.html` |
| Skill Workshop | `extensions/skill-workshop/`, `extensions/skill-workshop/src/{reviewer,scanner,signals,workshop}.ts` |
| Lobster | `extensions/lobster/`, `extensions/lobster/SKILL.md` |
| ACP bridge | `src/acp/`, `docs.acp.md` |
| Codex harness | `extensions/codex/src/app-server/`, `extensions/codex/openclaw.plugin.json` |
| Canvas / A2UI | `src/canvas-host/`, `src/canvas-host/a2ui/` |
| Voice Wake | `docs/nodes/voicewake.md` |
| Talk Mode | `docs/nodes/talk.md` |
| Voice Call | `extensions/voice-call/` |
| Multi-agent | `docs/concepts/multi-agent.md`, `docs/concepts/delegate-architecture.md` |
| Cron/Heartbeat/Tasks | `src/cron/`, `src/tasks/`, `docs/automation/cron-jobs.md`, `docs/automation/tasks.md` |
| Standing Orders | `docs/automation/standing-orders.md` |
| TaskFlow | `docs/automation/taskflow.md` |
| Hooks | `docs/automation/hooks.md` |
| Diagnostics OTEL | `extensions/diagnostics-otel/src/service.ts` |
| Sandbox Dockerfiles | `Dockerfile.sandbox`, `Dockerfile.sandbox-browser`, `Dockerfile.sandbox-common` |
| Streaming/queue | `docs/concepts/streaming.md`, `docs/concepts/queue.md` |
| iOS app | `apps/ios/Sources/` (17 modules + WatchApp + WatchExtension + ActivityWidget + ShareExtension) |
| Android app | `apps/android/app/src/`, `apps/android/benchmark/` |
| macOS app | `apps/macos/Sources/{OpenClaw, OpenClawDiscovery, OpenClawIPC, OpenClawMacCLI, OpenClawProtocol}` |
| macOS MLX TTS | `apps/macos-mlx-tts/Sources/OpenClawMLXTTSHelper/` |
| Bundled skills | `skills/` (53 entries) |
| TUI | `src/tui/` |
| Wizard/onboard | `src/wizard/` |
| Config docs | `docs/gateway/configuration.md`, `docs/gateway/configuration-reference.md` |

---

*End of survey. Total bundled extensions: 117. Total bundled skills: 53. Total docs files: 250+.*
