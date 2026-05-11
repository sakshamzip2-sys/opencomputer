# OC Feature Gaps: What to Take from OpenClaw

Deep analysis of OpenClaw (370k GitHub stars, production-grade personal AI gateway) vs what OC ships today.
Excludes: iOS/Android nodes, Canvas visual output (deprioritized).

---

## TIER 1 - Build These Now (Highest Impact)

---

### 1. Heartbeat / Proactive Loop

**What OpenClaw does:**
A `heartbeat` config block runs periodic agent turns in the `main` session on a configurable interval (default 30m). It reads an optional `HEARTBEAT.md` checklist from the workspace, decides if anything needs attention, and either sends an alert or replies `HEARTBEAT_OK` (which is silently dropped). Key options:

```json
{
  "agents": {
    "defaults": {
      "heartbeat": {
        "every": "30m",
        "target": "last",
        "lightContext": true,
        "isolatedSession": true,
        "skipWhenBusy": true,
        "activeHours": { "start": "08:00", "end": "22:00", "timezone": "Asia/Kolkata" },
        "prompt": "Read HEARTBEAT.md. If nothing needs attention, reply HEARTBEAT_OK.",
        "ackMaxChars": 300
      }
    }
  }
}
```

- `lightContext: true` - only injects HEARTBEAT.md, not the full conversation history (cheap to run)
- `isolatedSession: true` - fresh session per run, no history bleed
- `skipWhenBusy: true` - defers when a subagent or cron job is active
- `activeHours` - timezone-aware window; no nighttime spam
- `HEARTBEAT_OK` is stripped and the reply is dropped if it is below `ackMaxChars` - zero noise for quiet periods
- Tool-capable heartbeats can call `heartbeat_respond` with `notify: true/false` for structured alerts
- Per-agent heartbeat overrides possible: different agent, different interval, different channel

**What OC has today:**
Static cron jobs. No ambient self-driven loop that observes state and decides autonomously.

**What OC should build:**
- A `heartbeat` config block in `openclaw.json` / agent config
- Runs a lightweight agent turn every N minutes against the main session
- Reads `HEARTBEAT.md` from workspace
- Delivers to last-used channel or a specific target
- `HEARTBEAT_OK` protocol: silent drop for quiet periods
- `activeHours` window with timezone support
- `lightContext` mode: minimal system prompt, only heartbeat file
- `skipWhenBusy`: check if a cron or delegate is already running and defer
- Integrate with OC's existing `cron` tool as the scheduler backend

---

### 2. Model Failover Chain

**What OpenClaw does:**
Full two-stage failover:

1. **Auth profile rotation**: Within a provider, rotates across multiple API keys / OAuth tokens on rate limits, timeouts, or auth errors. Round-robin, oldest-used-first, cooldown tracking.
2. **Model fallback chain**: If the provider is exhausted, walks to the next model in `fallbacks[]`.

```json
{
  "agents": {
    "defaults": {
      "model": {
        "primary": "anthropic/claude-opus-4-5",
        "fallbacks": [
          "anthropic/claude-sonnet-4-20250514",
          "openai/gpt-4o",
          "google/gemini-2.0-flash"
        ]
      }
    }
  }
}
```

Runtime behavior:
- Persists the fallback choice to session so other readers see the same model
- On failure, rolls back only the model-selection fields it owns (not unrelated session state)
- `FallbackSummaryError` thrown with per-attempt detail and soonest cooldown expiry when all candidates exhausted
- Session stickiness: pins the chosen auth profile per session to keep provider caches warm
- Heartbeat runs without explicit `heartbeat.model` clear auto overrides when configured default changes
- Rate-limit bucket is broader than just 429 - also catches `Too many concurrent requests`, `ThrottlingException`, `workers_ai quota limit exceeded`, `weekly/monthly limit reached`, etc.
- Per-cron-job model overrides with their own fallback lists

**What OC has today:**
Single model. If Anthropic is down or rate-limited, OC is dead.

**What OC should build:**
- `model.primary` + `model.fallbacks[]` in agent config
- Wrap every model call with a `runWithModelFallback()` function
- Auth profile rotation: support multiple API keys per provider, rotate on 429/503
- Cooldown tracking per profile (in-memory, cleared on restart or after TTL)
- `FallbackSummaryError` with detail on which providers were tried
- Session stickiness: once a session picks a profile, stick with it until session reset or compaction
- This is genuinely a 1-2 day build and it massively improves reliability

---

### 3. Structured Secrets Management (SecretRefs)

**What OpenClaw does:**
Additive secret references - credentials never stored as plaintext in config files. Three source types:

```json
{
  "secrets": {
    "providers": {
      "default": { "source": "env" },
      "vault": {
        "source": "exec",
        "command": "/opt/homebrew/bin/vault",
        "args": ["kv", "get", "-field=OPENAI_API_KEY", "secret/openclaw"],
        "passEnv": ["VAULT_ADDR", "VAULT_TOKEN"]
      },
      "onepassword": {
        "source": "exec",
        "command": "/opt/homebrew/bin/op",
        "args": ["read", "op://Personal/OpenClaw/password"],
        "allowSymlinkCommand": true,
        "trustedDirs": ["/opt/homebrew"]
      }
    }
  }
}
```

- `source: "env"` - env var reference (what OC does today, but formalized)
- `source: "file"` - JSON pointer into a local secrets file
- `source: "exec"` - arbitrary CLI (1Password `op`, HashiCorp Vault, `sops`, anything)
- Resolution is **eager at startup**, not lazy on request paths - provider outages stay off the hot path
- Atomic swap on reload: full success or keep last-known-good
- `secrets audit --check` reports plaintext findings, unresolved refs, shadowing
- `secrets configure` is an interactive helper to migrate
- Startup fails fast if any ref cannot be resolved
- If ref and plaintext both exist, ref wins at runtime (plaintext ignored, warning logged)
- Exec provider: validated binary path, no shell, stdin JSON RPC protocol, timeout + output byte limits

**What OC has today:**
Raw env vars in `~/.zshrc`. Saksham's `CLAUDE_CODE_OAUTH_TOKEN` is in shell history and `.zshrc` in plaintext. This is a security issue, not just a missing feature.

**What OC should build:**
- SecretRef contract: `{ source, provider, id }` on any credential field
- `env` provider: formalize what OC does already
- `exec` provider: arbitrary CLI resolver (1Password, Vault, sops)
- `file` provider: local encrypted JSON file with JSON-pointer addressing
- Eager resolution at startup, atomic swap
- `oc secrets audit` CLI command
- Scrub known secret-like fields from `.zshrc` / config files when migrating
- First step: move all OC keys to `~/.opencomputer/.env` and reference via `${VAR}` syntax

---

### 4. Skill Requirements Gating (requires: frontmatter)

**What OpenClaw does:**
Skills declare what they need. OpenClaw checks before activating. If requirements are unmet, the skill is silently skipped rather than loading and failing at runtime.

```yaml
---
name: pdf-tool
description: Extract text from PDF files
requires:
  binaries: ["pdftotext", "ghostscript"]
  env: ["ADOBE_API_KEY"]
  os: ["macos", "linux"]
  plugins: ["unbrowse-openclaw"]
---
```

Skills config also has a rich per-skill config block:
```json
{
  "skills": {
    "entries": {
      "nano-banana-pro": {
        "apiKey": "${NANO_BANANA_KEY}"
      }
    }
  }
}
```

**What OC has today:**
Skills load regardless of whether their dependencies exist. They fail at runtime, often mid-task with an unhelpful error.

**What OC should build:**
- Add `requires:` block to SKILL.md frontmatter: `binaries`, `env`, `os`, `plugins`
- On session start, check requirements before injecting skill descriptions into context
- Silently skip unmet skills (don't surface them to the model at all)
- Log skipped skills in verbose mode so Saksham can see what's inactive and why
- This is cheap to implement - mostly frontmatter parsing + shell `command -v` checks

---

### 5. Deterministic Session-to-Agent Binding

**What OpenClaw does:**
8-tier binding priority system. Each inbound message is matched to exactly one agent using a priority chain:

1. Exact channel+peer binding
2. Channel-wide binding
3. Thread binding
4. Group-specific binding
5. Account-specific binding
6. Agent default
7. Channel default
8. Global default

Config example:
```json
{
  "agents": {
    "list": [
      { "id": "work", "model": { "primary": "anthropic/claude-opus-4-5" }, "workspace": "/Users/saksham/work" },
      { "id": "personal", "model": { "primary": "anthropic/claude-haiku-4-5" } }
    ]
  },
  "channels": {
    "telegram": {
      "groups": {
        "-1001234567": { "agent": "work" }
      }
    },
    "discord": {
      "channels": {
        "general": { "agent": "personal" }
      }
    }
  }
}
```

Each agent gets:
- Isolated workspace
- Isolated session history
- Isolated model selection
- Isolated tool permissions
- Own heartbeat config

**What OC has today:**
Single agent. Multi-agent is ad-hoc delegation via the `delegate` tool. No config-driven routing.

**What OC should build:**
- `agents.list[]` with per-agent model, workspace, tools, and heartbeat
- Channel routing config: `channel -> agent` mappings
- Session isolation: each agent keeps its own session history
- Named agent profiles (work, personal, code) switchable via `/agent <id>` in chat
- This enables Saksham to have a work OC (different model, different workspace, different soul) and a personal OC on separate Telegram chats

---

## TIER 2 - High Value, Build After Tier 1

---

### 6. Lobster: Deterministic Workflow Pipelines

**What OpenClaw does:**
Lobster is a workflow shell for running multi-step tool sequences as single, deterministic, resumable operations with explicit approval checkpoints.

```json
{
  "action": "run",
  "pipeline": "exec --json --shell 'inbox list --json' | exec --stdin json --shell 'inbox categorize --json' | approve --preview-from-stdin --limit 5 --prompt 'Apply changes?'",
  "timeoutMs": 30000
}
```

- JSON pipes between steps: `stdin: $step.stdout`
- Approval gates: workflow pauses, returns a `resumeToken`, you call resume to continue
- Resumable without re-running already-completed steps
- Runs in-process (no external subprocess)
- `.lobster` YAML/JSON workflow files with `args`, `steps`, `env`, `condition`, `approval` fields
- Integrates with `llm-task` for structured LLM steps inside a deterministic pipeline
- Safety: timeouts, output caps, sandbox checks enforced by the runtime not each script

**What OC has today:**
Multi-step workflows are done via the LLM orchestrating tool calls. Non-deterministic. No approval checkpoints. No resume tokens.

**What OC should build:**
- A `Lobster`-equivalent workflow runner (can be simpler initially)
- YAML pipeline spec: steps, conditions, approval gates
- `resumeToken` protocol: pause on approval-required steps, resume without re-running
- Pipe JSON between steps
- Integrate with OC's existing cron and delegate tools
- Approval gate UI: push to Telegram/Discord/iMessage for human confirm

---

### 7. Tool-Loop Detection

**What OpenClaw does:**
Two cooperating guardrails:

1. **Rolling-history detector** (opt-in): watches last N tool calls for repeated patterns
   - `genericRepeat`: same tool + same args, repeated
   - `knownPollNoProgress`: known polling-like patterns with no state change
   - `pingPong`: alternating A->B->A->B patterns
   - Warning threshold -> critical threshold -> global circuit breaker
   - Per-agent override of thresholds

2. **Post-compaction guard** (on by default): arms after every compaction-retry, aborts if same `(tool, args, result)` triple appears N times in the window

```json
{
  "tools": {
    "loopDetection": {
      "enabled": true,
      "historySize": 30,
      "warningThreshold": 10,
      "criticalThreshold": 20,
      "globalCircuitBreakerThreshold": 30,
      "detectors": {
        "genericRepeat": true,
        "knownPollNoProgress": true,
        "pingPong": true
      },
      "postCompactionGuard": { "windowSize": 3 }
    }
  }
}
```

**What OC has today:**
No loop detection. A runaway agent can burn unlimited tokens in a compaction loop.

**What OC should build:**
- Post-compaction guard first (lowest effort, highest safety payoff)
- Rolling-history detector as optional feature
- Configurable thresholds
- `compaction_loop_persisted` error with the offending tool name when guard triggers

---

### 8. Tokenjuice: Tool Result Compaction

**What OpenClaw does:**
An optional bundled plugin (`tokenjuice`) that compacts noisy exec/bash tool results **after the command has run**, before feeding them back into the session. It changes the `tool_result`, not the command itself.

- Trims verbose build output, git status noise, long stack traces to essential signal
- Preserves exact file-content reads (leaves those raw)
- Opt-in: disable if you want verbatim output

**What OC has today:**
All tool results go into context verbatim. A `find / -name "*.py"` result can burn 50k tokens.

**What OC should build:**
- A `tool_result` middleware layer
- Configurable per tool type: what gets compacted vs left raw
- Compaction strategy: keep first N lines, extract error lines, summarize the rest
- Hook into OC's existing `Bash` and `Read` tools

---

### 9. Trajectory Bundles (Session Flight Recorder)

**What OpenClaw does:**
Per-session flight recorder. Writes a structured timeline for every agent run:

- Runtime events: `session.started`, `context.compiled`, `prompt.submitted`, `model.fallback_step`, `model.completed`, `session.ended`
- Transcript events: user messages, assistant messages, tool calls, tool results, compactions, model changes
- `/export-trajectory` packages the current session into a redacted support bundle
- Bundle files: `events.jsonl`, `session-branch.json`, `metadata.json`, `artifacts.json`, `prompts.json`, `system-prompt.txt`, `tools.json`
- Redacts credentials, image data, local paths before writing
- Bounded: stops at 10MB per sidecar, 200k events max

**What OC has today:**
Session history is stored but there's no structured per-run event timeline. Debugging a failed run requires reading raw session JSON.

**What OC should build:**
- `trajectory.jsonl` sidecar per session
- Events: prompt submitted, tool calls, tool results, compaction, model switches, errors
- `/export-trajectory` command that packages it into a redacted bundle
- Use for: debugging, support, replay

---

### 10. Broadcast Groups

**What OpenClaw does:**
A single inbound message triggers all listed agents simultaneously. Each agent maintains isolated session, history, workspace. Currently WhatsApp-scoped but architecture is general.

```json
{
  "channels": {
    "whatsapp": {
      "broadcastGroups": {
        "-1001234567": ["work", "research", "monitor"]
      }
    }
  }
}
```

**What OC has today:**
One message -> one agent. Parallel agents require explicit `delegate` calls inside a run.

**What OC should build:**
- `broadcastGroups` config: chat ID -> list of agent IDs
- Dispatch to all listed agents when a message hits that chat
- Collect and merge/separate their responses
- Useful for: running a personal agent AND a logging agent AND a task-extraction agent on every message

---

### 11. Standing Orders

**What OpenClaw does:**
Grant the agent permanent operating authority for defined programs. Instead of re-issuing task instructions each time, define programs with clear scope, triggers, and escalation rules. Agent executes autonomously within those boundaries.

Example: "Every time you see a GitHub notification mentioning 'urgent', create a Linear issue and ping me on Telegram." Set once, runs forever.

**What OC has today:**
No persistent behavioral rules outside of cron jobs and skills. You have to re-instruct per session.

**What OC should build:**
- A `standing_orders.md` or config block that persists operational rules
- Rules evaluated by the agent at run start and during heartbeat
- Escalation rules: what to do vs what to ask the human
- Scope limits: which tools the standing order can use autonomously

---

### 12. Thinking Levels (Per-Run Effort Control)

**What OpenClaw does:**
`/think low|medium|high` in chat sets the thinking budget for the active run. Also configurable per heartbeat, per cron job, per subagent:

```json
{ "thinking": "high" }
```

- `low`: fast, cheap, for simple tasks
- `medium`: default
- `high`: extended thinking, expensive, for hard problems
- Per-run, not global - you don't pay for extended thinking on every heartbeat

**What OC has today:**
Extended thinking is available via Anthropic API but there's no in-session control surface. You have to change it at the model call level.

**What OC should build:**
- `/think low|medium|high` slash command that sets thinking budget for the current run
- `thinking` field in cron job payloads
- `thinking` field in heartbeat config
- Map to Anthropic's `thinking: { type: "enabled", budget_tokens: N }` parameter

---

### 13. Steer: In-Flight Agent Redirection

**What OpenClaw does:**
`/steer <message>` injects a message into an **active running** agent at the next supported runtime boundary. Different from `/queue` which buffers messages for after the current run.

Use case: agent is going down the wrong path 3 minutes into a long task - you steer it without aborting and restarting.

**What OC has today:**
No in-flight steering. You either wait for the run to finish or abort it.

**What OC should build:**
- Inject user messages into the active agent loop at safe checkpoints (between tool calls)
- `/steer <message>` command
- The agent sees the steer message as a new user turn and can course-correct

---

### 14. Exec Approvals (Granular, Per-Command)

**What OpenClaw does:**
Fine-grained approval control per command type. Not just "allow all bash" or "deny all bash" - specific patterns:

```json
{
  "tools": {
    "exec": {
      "approvals": {
        "git commit": "allow",
        "git push": "ask",
        "rm -rf": "deny",
        "npm install": "ask"
      }
    }
  }
}
```

- `allow`: run without asking
- `ask`: show prompt, user confirms
- `deny`: block outright
- Per-session `allow-always` that persists to config
- Group chats get stricter defaults than direct messages

**What OC has today:**
Binary consent gate: entire tool classes are allowed or denied. No per-command pattern matching.

**What OC should build:**
- Pattern-based permission rules for Bash tool
- `allow`, `ask`, `deny` per glob pattern
- `allow-always` option that writes the rule to config
- Different defaults for different contexts (auto mode vs interactive)

---

## TIER 3 - Infrastructure and Operations

---

### 15. ACP: External Harness Protocol

**What OpenClaw does:**
Agent Client Protocol (ACP) lets OpenClaw spawn and manage external coding harnesses (Claude Code, Codex, Gemini CLI, Cursor, OpenCode, Copilot, etc.) as tracked background tasks with chat-bound sessions.

```
/acp spawn claude --bind here
/acp spawn gemini --mode persistent --thread auto
```

- Each ACP session is a background task with its own session key
- Can bind to the current conversation thread
- Delivery model: responses routed back to the bound chat
- OpenClaw controls routing, background-task state, delivery; the harness controls its own provider auth and tools
- `openclaw mcp serve`: expose an OC Gateway session as an MCP server for editors

**What OC has today:**
The `delegate` tool spawns subagents. No protocol for integrating external harnesses (Claude Code, Codex, etc.) as first-class peers.

**What OC should build:**
- ACP adapter: spawn Claude Code / Codex as a managed subprocess with session binding
- Route responses back to the originating chat
- Background task tracking for long-running harness sessions
- `/acp spawn`, `/acp status`, `/acp stop` commands

---

### 16. Gateway Health Dashboard

**What OpenClaw does:**
Built-in web dashboard at the gateway address showing:
- Agent health (is the heartbeat running, last run status)
- Session counts and active sessions
- Message traffic per channel
- Model usage and costs
- Memory file browser
- System health (CPU, memory, uptime)

Also: Prometheus endpoint, OpenTelemetry export, structured logging.

**What OC has today:**
Completely headless. You dig into logs or call CLI commands.

**What OC should build:**
- Web dashboard at `localhost:18789/dashboard` (or similar)
- Session list, active runs, cron status
- Token usage and cost tracking
- Prometheus metrics endpoint for external monitoring

---

### 17. Sandboxed Tool Execution

**What OpenClaw does:**
Tools can run inside isolated Docker containers (or SSH backends) instead of directly on the host. Per-agent configuration:

```json
{
  "agents": {
    "defaults": {
      "sandbox": {
        "mode": "non-main"
      }
    }
  }
}
```

- `mode: "non-main"`: sandbox all sessions except the main session
- Docker backend: containerized exec with configurable allowlists
- SSH backend: run tools on a remote host
- Built-in tool allowlist per sandbox: allow `bash, read, write` but deny `browser, nodes, cron`
- Trusted main session runs on host; untrusted/group sessions run sandboxed

**What OC has today:**
All Bash runs directly on Saksham's machine. No isolation.

**What OC should build:**
- Optional Docker sandbox backend for Bash tool execution
- Per-session or per-agent sandbox mode config
- Tool allowlist/denylist per sandbox level
- Start with: a configurable denylist of dangerous patterns as a minimum viable safety layer

---

### 18. Multi-Account Channel Support

**What OpenClaw does:**
Multiple accounts per channel. Each account has its own bot token / credentials. Messages from different accounts route to different agents.

```json
{
  "channels": {
    "telegram": {
      "accounts": {
        "personal-bot": { "botToken": "${TELEGRAM_PERSONAL_TOKEN}" },
        "work-bot": { "botToken": "${TELEGRAM_WORK_TOKEN}" }
      }
    }
  }
}
```

**What OC has today:**
Single account per channel type.

**What OC should build:**
- `accounts` map per channel
- Each account independently authenticated
- Routing: account -> agent mapping

---

### 19. Plugin SDK for Channel Adapters

**What OpenClaw does:**
Channels are pluggable. Matrix, Mattermost, Microsoft Teams, Nostr, Twitch, Zalo are all bundled plugins. Third parties can author new channel adapters using the Plugin SDK.

```typescript
// Plugin SDK
import { Plugin } from "@openclaw/plugin-sdk";
const plugin: Plugin = {
  id: "my-channel",
  channels: [{ id: "mychat", ... }]
};
```

**What OC has today:**
Platform adapters are hard-coded. Adding a new channel requires touching core OC code.

**What OC should build:**
- A plugin SDK contract for channel adapters
- Channel adapter interface: `receive(message)`, `send(message)`, `auth()`
- Plugin discovery: scan `~/.opencomputer/plugins/`
- This is a longer build but enables community-contributed channels

---

### 20. Context Pruning Modes

**What OpenClaw does:**
Multiple context pruning strategies configurable per agent:

```json
{
  "agents": {
    "defaults": {
      "contextPruning": {
        "mode": "cache-ttl",
        "ttl": "1h"
      },
      "compaction": {
        "mode": "safeguard"
      }
    }
  }
}
```

- `mode: "cache-ttl"`: prune context entries older than TTL
- `mode: "sliding"`: keep last N turns verbatim
- `mode: "none"`: no pruning (let it grow, rely on compaction)
- `compaction.mode: "safeguard"`: compact at 98% context, preserving critical state
- `compaction.mode: "aggressive"`: compact earlier and more aggressively
- `lightContext` on heartbeat: minimal system prompt, only HEARTBEAT.md injected

**What OC has today:**
Compaction triggers at 98% context (inherited from Claude Code). No configurable pruning mode.

**What OC should build:**
- `contextPruning.mode` config option
- `sliding` mode: keep last N turns verbatim as a cheaper alternative to full compaction
- `lightContext` mode for scheduled runs: build a minimal system prompt to save tokens
- Expose compaction threshold as configurable (not just hardcoded at 98%)

---

## Priority Order for Implementation

| # | Feature | Effort | Impact |
|---|---------|--------|--------|
| 1 | Model Failover Chain | 1-2 days | Critical reliability |
| 2 | SecretRefs / Secrets Management | 2-3 days | Security fix (UNRESOLVED issue) |
| 3 | Heartbeat / Proactive Loop | 3-5 days | UX step-change |
| 4 | Skill Requirements Gating | 1 day | Quality of life |
| 5 | Tool-Loop Detection (post-compaction guard) | 1 day | Safety |
| 6 | Tokenjuice | 1-2 days | Cost reduction |
| 7 | Thinking Levels (per-run) | 1 day | Control |
| 8 | Steer (in-flight steering) | 2-3 days | UX |
| 9 | Exec Approvals (pattern-based) | 2 days | Safety |
| 10 | Session-to-Agent Binding | 3-5 days | Multi-persona |
| 11 | Trajectory Bundles | 2-3 days | Debugging |
| 12 | Standing Orders | 3-5 days | Automation |
| 13 | Lobster Pipelines | 5-7 days | Power feature |
| 14 | Broadcast Groups | 3 days | Multi-agent |
| 15 | Context Pruning Modes | 2 days | Cost control |
| 16 | ACP External Harnesses | 5-7 days | Ecosystem |
| 17 | Gateway Dashboard | 5-7 days | Operations |
| 18 | Sandboxed Execution | 5-7 days | Safety |
| 19 | Multi-Account Channels | 3 days | Scale |
| 20 | Plugin SDK (channels) | 2+ weeks | Ecosystem |
