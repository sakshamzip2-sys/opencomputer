# OpenComputer — Session Context for Claude Code

This file is auto-loaded at session start. It is the **single comprehensive brief** a new Claude session needs to resume work on OpenComputer without re-explaining anything.

Last updated: 2026-05-08 (browser-harness is the default browser layer; opencli-bridge added as a complementary plugin shipping 100+ deterministic site adapters + auto-loaded chrome.debugger extension; legacy browser-control plugin is dormant — files retained, register() short-circuits without registering tools; reactivate via OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY=1)

---

## 1. Project elevator pitch

**OpenComputer** is a personal AI agent framework, written in Python 3.12+, that synthesizes the best ideas from four reference projects into one cohesive system:

| Reference | What we took |
|---|---|
| [Claude Code](https://github.com/anthropics/claude-code) | Plugin primitives (commands/skills/agents/hooks/MCP), lifecycle events, tool shapes (Edit, MultiEdit, TodoWrite) |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Python core patterns, three-pillar memory (declarative + procedural + episodic), agent loop shape, channel adapter pattern, Jinja2 prompt templating (shared with Kimi) |
| [OpenClaw](https://github.com/openclaw/openclaw) | Plugin-first architecture, strict SDK boundary, manifest-first two-phase discovery (scan cheap metadata, activate lazily), typed wire protocol |
| [Kimi CLI](https://github.com/MoonshotAI/kimi-cli) | Dynamic injection providers for cross-cutting modes, fire-and-forget hooks, deferred MCP loading, StepOutcome abstraction |

**Positioning:** "Same agent, same memory. Install the coding-harness plugin → it's a coding agent. Don't install → it's a chat agent. Your choice." Works from CLI, Telegram, Discord, and any WebSocket client (TUI, IDE).

Identity is user-configurable, not locked:
- You are **Saksham** (GitHub: `sakshamzip2-sys`).
- Repo: `https://github.com/sakshamzip2-sys/opencomputer` (PUBLIC).
- Authored on macOS (darwin), zsh.

---

## 2. Repository layout

The parent git repo is at `/Users/saksham/Vscode/claude/` and contains the OpenComputer project plus four reference repos consolidated under `sources/` (gitignored so they don't pollute our commits):

```
/Users/saksham/Vscode/claude/
├── .git/                            ← parent repo — GitHub sakshamzip2-sys/opencomputer
├── .gitignore                       ← ignores sources/ + build artifacts
├── .github/workflows/
│   ├── test.yml                     ← pytest on Python 3.12 + 3.13 on every push/PR
│   ├── lint.yml                     ← ruff check
│   └── release.yml                  ← triggered on v* tags, publishes to PyPI (OIDC)
├── OpenComputer/                    ← THE PROJECT. cd here for anything code-related.
│   └── docs/refs/                   ← reference-extraction notes by repo
│       ├── claude-code/
│       ├── hermes-agent/
│       ├── openclaw/
│       └── kimi-cli/
└── sources/                         ← reference repos (gitignored — not in commits)
    ├── claude-code/
    ├── hermes-agent/
    ├── openclaw/
    └── kimi-cli/
```

### OpenComputer/ structure

```
OpenComputer/
├── pyproject.toml                   ← hatchling build, deps, ruff/pytest config
├── README.md                        ← user-facing docs
├── CLAUDE.md                        ← THIS FILE
├── AGENTS.md                        ← dev guide for AI assistants
├── RELEASE.md                       ← runbook for cutting a release
├── CHANGELOG.md                     ← Keep-a-Changelog format
├── .venv/                           ← local development venv (gitignored)
│
├── opencomputer/                    ← CORE PACKAGE (can be refactored freely)
│   ├── __init__.py                  ← __version__ = "0.1.0"
│   ├── cli.py                       ← Typer CLI — 15 subcommands + profile/plugin/preset/memory/mcp groups
│   ├── doctor.py                    ← opencomputer doctor — health checks
│   ├── setup_wizard.py              ← opencomputer setup — onboarding
│   ├── agent/
│   │   ├── loop.py                  ← AgentLoop.run_conversation — THE while loop
│   │   ├── state.py                 ← SessionDB (SQLite + FTS5 full-text search)
│   │   ├── memory.py                ← MemoryManager (declarative + procedural)
│   │   ├── config.py                ← typed dataclasses: Model/Loop/Session/Memory/MCP
│   │   ├── config_store.py          ← load/save ~/.opencomputer/config.yaml
│   │   ├── injection.py             ← InjectionEngine — collects mode providers per turn
│   │   ├── compaction.py            ← CompactionEngine (auto-summarize when context full)
│   │   ├── step.py                  ← StepOutcome dataclass
│   │   ├── prompt_builder.py        ← Jinja2 prompt rendering
│   │   └── prompts/base.j2          ← default system prompt template
│   ├── tools/                       ← built-in tools
│   │   ├── registry.py              ← ToolRegistry singleton + dispatch
│   │   ├── read.py, write.py, bash.py, grep.py, glob.py
│   │   ├── skill_manage.py          ← self-improvement: agent saves skills
│   │   └── delegate.py              ← spawn subagent with isolated context
│   ├── gateway/                     ← messaging gateway + wire server
│   │   ├── server.py                ← Gateway daemon (Telegram/Discord etc.)
│   │   ├── dispatch.py              ← MessageEvent → AgentLoop routing + typing heartbeat
│   │   ├── protocol.py              ← WireRequest/Response/Event (pydantic)
│   │   └── wire_server.py           ← WebSocket JSON-RPC for TUI/IDE clients
│   ├── hooks/
│   │   ├── engine.py                ← Hook dispatcher (6 events)
│   │   └── runner.py                ← fire-and-forget async runner (kimi pattern)
│   ├── plugins/                     ← plugin system (not plugins themselves!)
│   │   ├── discovery.py             ← scans manifests → PluginCandidates (cheap)
│   │   ├── loader.py                ← imports entry module + runs register(api)
│   │   └── registry.py              ← PluginRegistry singleton + PluginAPI
│   ├── mcp/
│   │   └── client.py                ← MCPTool + MCPManager (deferred load)
│   └── skills/
│       └── debug-python-import-error/SKILL.md   ← first bundled skill
│
├── plugin_sdk/                      ← PUBLIC CONTRACT. Plugins import from here ONLY.
│   │                                  NEVER imports from opencomputer/*.
│   │                                  Linter test enforces this.
│   ├── __init__.py                  ← ~30 public exports
│   ├── core.py                      ← Message, ToolCall, ToolResult, Platform, MessageEvent
│   ├── tool_contract.py             ← BaseTool, ToolSchema
│   ├── provider_contract.py         ← BaseProvider, ProviderResponse, StreamEvent, Usage
│   ├── channel_contract.py          ← BaseChannelAdapter
│   ├── hooks.py                     ← HookSpec, HookContext, HookDecision (6 events)
│   ├── injection.py                 ← DynamicInjectionProvider ABC, InjectionContext
│   └── runtime_context.py           ← RuntimeContext (plan_mode, yolo_mode, custom)
│
├── extensions/                      ← 7 bundled plugins
│   ├── telegram/                    ← kind=channel. TELEGRAM_BOT_TOKEN via env
│   ├── discord/                     ← kind=channel. DISCORD_BOT_TOKEN
│   ├── anthropic-provider/          ← kind=provider. x-api-key + Bearer-proxy support
│   ├── openai-provider/             ← kind=provider. OpenAI + OpenAI-compatible endpoints
│   ├── coding-harness/              ← kind=mixed. Edit/MultiEdit/TodoWrite/bg/plan-mode
│   ├── dev-tools/                   ← kind=tools. Phase 12d.1 — porcelain dev utilities
│   └── memory-honcho/               ← kind=memory. Phase 10f.K–N — Honcho overlay (opt-in)
│
├── tests/                           ← 809 tests, all passing (59 test files)
│
└── docs/                            ← reference notes + author guides (Sub-project B populates more)
```

---

## 3. Architecture in one diagram

```
                    user
                      │
   ┌──────────────────┼──────────────────────┐
   │                  │                       │
   ▼                  ▼                       ▼
opencomputer    opencomputer            opencomputer
   chat          gateway                    wire
(streaming      (daemon with              (WS server
 CLI tokens)    channel adapters)          for TUI/IDE)
   │                  │                       │
   └──────────────────┼──────────────────────┘
                      │
                      ▼
                ╔═══════════╗
                ║ AgentLoop ║  ← run_conversation(user_msg, runtime)
                ╠═══════════╣
                ║ • inject (plan/yolo modes via InjectionEngine)
                ║ • compact (auto-summarize old turns when full)
                ║ • call provider.complete() or stream_complete()
                ║ • dispatch tool calls in parallel (safety-checked)
                ║ • fire PreToolUse hooks (can block)
                ║ • loop until model stops calling tools
                ╚═══════════╝
                      │
                      ▼
          ┌───────────────────────────┐
          │  plugin_sdk/ (PUBLIC)     │   ← 30 exports
          │  Stable contract.         │
          └───────────────────────────┘
                      ▲
                      │ (plugins import from here)
                      │
          ┌───────────┼───────────┬─────────────┐
          │           │           │             │
       telegram    discord   anthropic      coding-
                              openai       harness
```

**The rule:** plugins never import from `opencomputer/*`. Only from `plugin_sdk/*`. Enforced by a test that scans plugin_sdk/ for any `from opencomputer` imports.

---

## 4. What's been built (all phases to date)

All committed + pushed to `main`. Current main sha: `5c62a12` (2026-04-24).

| Phase | PR / Commit | What |
|---|---|---|
| 0 | `0d512cb` | Project scaffold — folder structure, pyproject, smoke tests |
| 1 | `8d96aff` | Core: agent loop, SQLite+FTS5, 3 tools, Anthropic provider |
| 1.5 | `11209c9` | skill_manage, Grep, Glob, delegate, hook engine, plugin discovery |
| 2 | `4252f17` | Gateway + Telegram (first real plugin) |
| 2.1 | `c280dc6` | Bearer auth + x-api-key strip for Claude Router proxy |
| 3 | `eb22d46` | OpenAI provider plugin + plugin-registry provider resolution |
| 3.1 | `441690d`, `be42ff8` | Anthropic moved to plugin + config command + loader cache fix |
| 4 | `37642be` | MCP integration + bundled skills path |
| 5 | `684226a` | Generic-ify — setup wizard, doctor, clean README |
| 6a | `c739c4a` | Injection + compaction engines + RuntimeContext threading |
| 6b | `bfa1ada` | coding-harness plugin — Edit, MultiEdit, TodoWrite, bg processes, plan mode |
| 7 | `96b1b7d` | Real streaming in both providers + Telegram typing heartbeat |
| 8 | `e9240da` | Discord channel plugin |
| 9 | `d5802c8` | WebSocket wire server + RPC protocol dispatch |
| 10a | `01a8f9c` | CI/CD (GitHub Actions) + ruff configuration + codebase cleanup |
| 10b | `2858815` | PyPI release automation + v0.1.0 prep |
| 10e | PR #2 / `00379e1` | WebFetch + WebSearch tools (2026-04-23) |
| 10f.K–N | PRs #13 / #15 / #16 | Honcho memory overlay — plugin skeleton, wizard step, host key per profile |
| 11a | PR #3 | Inventory / parity tracker |
| 11b | PR #4 | Claude-code parity: NotebookEdit, SkillTool, PushNotification, AskUserQuestion |
| 11c | PR #5 | MCP expansion — install-from-preset, catalog groundwork |
| 11d | PR #6 | Episodic memory + Anthropic batch integration |
| 12a | PR #18 / `1c08508` | Recall tool + post-response reviewer + agent cache (Tier 1 memory loop, 2026-04-23) |
| 12d.1 | PR #12 | `dev-tools` plugin (porcelain dev utilities) |
| 12d.2 | PR #17 / `545bf20` | Multi-provider WebSearch backend chain |
| 12f | PR #9 | 15 curated skills imported (superpowers + everything-claude-code subset) |
| 12g | PR #10 | SDK boundary hardening (test-enforced `plugin_sdk/` contract) |
| 14.A | `2ff243c`, `1b02f84` | Per-profile directory + pre-import `-p` flag routing |
| 14.B | `210599a` | `opencomputer profile` CLI (list/create/use/delete/rename/path) |
| 14.C | `9673100` | `PluginManifest.profiles` + `single_instance` fields in SDK |
| 14.D | `10300b4` | Layer A manifest profile enforcement in loader |
| 14.E | `ee90467` | Profile-local plugin dir + install/uninstall/where CLI |
| 14.J | PR #16 / `7169820` | Honcho host key derived from active profile + README limitations |
| 14.L | PR #14 / `ebb32db` | README Profiles / Presets / Plugin sections + CHANGELOG |
| 14.M | `7fc1185` | Named plugin-activation presets + CLI |
| 14.N | `0a829ca` | Workspace `.opencomputer/config.yaml` overlay |
| Sub-project A | PR #20 / `6ad86b5` | Honcho-as-default memory (A1-A8) — v1.0 ship-gate |
| Sub-project B | PR #21 / `e57d191` | `opencomputer plugin new` scaffolder (B1-B6) — v1.0 ship-gate |
| Sub-project D.1–3 | PR #22 / `9b55789` | Coding-harness Phase 6d-6f rebase + SDK boundary fix |
| Sub-project D.5+D.7 | PR #23 / `1227e19` | ExitPlanMode tool + PreCompact/SubagentStop/Notification hook emissions |
| Sub-project C | PR #24 / `f2e8f0f` | Profile parity with Hermes — `home/` + wrappers + `SOUL.md` (C1-C4) |
| Adversarial follow-ups | PR #25 / `89b1e84` | Follow-ups across PRs #25-#28 (test hardening, drift guards) |
| Sub-project E | PR #26 / `633c8eb` | Demand-driven plugin activation (E1-E6) |
| Sub-project D tail | PR #27 / `5c62a12` | Cheap-route gating (D6) + slash-command router formalization (D8) — v1.0 candidate (2026-04-24) |
| Drift preventers | PR #29 / `00bf48b` | Pre-v1.0 cleanup — PascalCase tool renames (CheckpointDiff, GitDiff, StartProcess, CheckOutput, KillProcess) + plugin search-path consolidation |
| Sub-project F1 | PR #?? / (pending) | Consent layer + immutable audit log (core, non-bypassable). Schema v1→v2, 4 SDK types, 8 CLI subcommands, HMAC-chained tamper-evident audit, progressive promotion (N=10), bypass flag, AGPL-isolation grep test. Infrastructure only — F2+ attach claims to tools |
| OI removal | 2026-04-27 (branch `feat/native-cross-platform-introspection`) | `oi_bridge` (Open Interpreter subprocess bridge, AGPL) replaced by native cross-platform `extensions/coding-harness/introspection/` module (psutil/mss/pyperclip/rapidocr-onnxruntime). 5 tool names preserved; F1 capability namespace migrated `oi_bridge.*` → `introspection.*`. Net diff ~−2,400 LOC. Cross-platform support extended from "macOS, Linux only" to "macOS, Linux, Windows" (psutil + os.walk replace the broken `ps aux` / `find -mmin` paths). `docs/f7/` removed. |

**Test count:** 885 passing across 71 test files.

**Bundled extensions (7):** telegram, discord, anthropic-provider, openai-provider, coding-harness, dev-tools, memory-honcho.

---

### 4.1 browser-harness (2026-05-08, DEFAULT — replaces legacy browser-control)

**Status:** active default. ``adapter-runner`` now routes browser ops through ``BrowserHarnessActions`` (Hermes-derived, agent-browser CLI). The legacy ``browser-control`` plugin is dormant — its package files remain on disk so the typed-error fallback in ``adapter-runner._ctx._typed_browser_errors`` still works as a path-3 backstop, and the ``extensions.browser_control`` package namespace is bootstrapped at import time for any straggler relative imports — but its ``register()`` returns early before any tools are registered. Reactivate the legacy path via ``OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY=1``.

**What it is:** a Hermes-derived multi-backend browser plugin lifted from `nousresearch/hermes-agent` `tools/browser_*.py`. Replaces the broken Playwright-based `browser-control` plugin with `agent-browser` CLI (Node, project-local install) plus pluggable cloud providers.

**Backends supported (all from Hermes):**
- Local headless Chromium via `agent-browser` (default)
- User's real Chrome via CDP (`OPENCOMPUTER_BROWSER_CDP_URL=ws://localhost:9222`)
- Browser Use Cloud (`BROWSER_USE_API_KEY`)
- Browserbase (`BROWSERBASE_API_KEY` + `BROWSERBASE_PROJECT_ID`)
- Firecrawl (`FIRECRAWL_API_KEY`)
- Camofox local stealth (`CAMOFOX_URL`)

Plus one OC-specific addition planned (extension-daemon for managed-Chrome reliability) — not yet implemented; the structural fix for the chat-mode-after-idle bug comes from agent-browser's process-isolated daemon rather than Playwright/CDP.

**Files:**
- `extensions/browser-harness/dispatcher.py` — lifted Hermes `browser_tool.py` (byte-identical except imports)
- `extensions/browser-harness/browser_camofox.py`, `browser_camofox_state.py` — Camofox client (lifted)
- `extensions/browser-harness/browser_providers/` — 4 cloud provider files (lifted)
- `extensions/browser-harness/redact.py` — Hermes secret-redaction module (lifted byte-identical)
- `extensions/browser-harness/compat.py` — Hermes→OC shims (real wires for `is_safe_url`/`load_config`/`get_hermes_home`; `call_llm` raises until OC's `auxiliary_client` is wired)
- `extensions/browser-harness/tools.py` — OC `BaseTool` wrappers: `BrowserNavigate`, `BrowserSnapshot`, `BrowserClick`, `BrowserType`, `BrowserVision`
- `extensions/browser-harness/actions.py` — `BrowserHarnessActions` adapter-runner client (drop-in for `extensions.browser_control.client.BrowserActions`)
- `extensions/browser-harness/config.py` — `detect_backend()` + `use_browser_harness_for_adapter_runner()` introspection
- `extensions/browser-harness/VENDORED.md` — provenance + divergence log

**External deps added:**
- `requests` Python package (`pip install requests`) — Hermes uses it for HTTP calls into cloud providers
- `node_modules/agent-browser` — installed via `npm install agent-browser` in OC repo root (project-local, NOT global)
- `node_modules/.bin` is prepended to PATH at plugin load by `plugin.py`

**Default behaviour:** `adapter-runner` routes browser ops through `browser-harness` automatically. No env var required.

```bash
opencomputer chat   # browser-harness handles browser tools by default
```

**Reverting to legacy browser-control:**
```bash
export OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY=1
opencomputer chat   # adapter-runner uses browser-control's BrowserActions (Playwright)
```
This is an emergency escape hatch only. The legacy plugin's `register()` short-circuits without setting this env var, so its tools stay invisible to the LLM. Setting it BOTH re-enables the legacy plugin's tool registration AND switches `adapter-runner` back to it.

**Persistent browser profile (OpenClaw-style, default since 2026-05-08):** `plugin.py` sets `AGENT_BROWSER_PROFILE=<oc_profile_home>/browser-profile/` at register time so agent-browser's Chromium uses a fixed user-data-dir per OC profile. Cookies, logins, extensions, and history persist across runs. Each `-p <name>` OC profile gets its own isolated browser profile. Users who export `AGENT_BROWSER_PROFILE` themselves before launch are left alone. Without this, agent-browser would default to an ephemeral `/var/folders/.../T/agent-browser-chrome-<uuid>/` dir per process — fresh dir on every daemon restart, all cookies lost.

**Headed vs. headless:** `agent-browser` runs headless by default. Set `AGENT_BROWSER_HEADED=1` to see the Chromium window (useful for first-time logins / debugging). Headless is the right default for production / batch runs.

**Known caveats:**
- `call_llm` is stubbed (raises `CallLLMNotConfigured`); both Hermes call sites have try/except fallbacks so vision analysis and content-extraction features degrade gracefully. Wiring to `opencomputer.agent.auxiliary_client` is a future enhancement.
- `check_website_access` returns None (no per-profile website allow/deny policy yet).
- agent-browser's persistent profile dir is separate from the user's real system Chrome (Google Chrome.app). User logins from system Chrome are NOT shared. For sites needing auth, log in once via agent-browser's headed Chromium and the cookies stick to that OC-profile-scoped dir thereafter.
- The Nous-managed-tool-gateway path was removed from `browser_providers/browser_use.py` (irrelevant to OC). Documented in `VENDORED.md` "Divergences" section.

**To deprecate legacy browser-control:** validate browser-harness against LearnX/Luma/Swiggy adapters end-to-end (requires LLM budget + initial agent-browser auth setup), then promote `OPENCOMPUTER_USE_BROWSER_HARNESS=1` to default. After that, delete `extensions/browser-control/` and drop the `playwright` Python dep.

---

### 4.2 opencli-bridge (2026-05-08, complementary to browser-harness)

**Status:** active default. Sibling browser-tool plugin alongside browser-harness. Bridges [`@jackwener/opencli`](https://github.com/jackwener/opencli) (Apache-2.0, 19k⭐, Node CLI) into OC. Provides 100+ pre-built deterministic site adapters (HN, Reddit, X/Twitter, Wikipedia, PyPI, Steam, GitHub, Bilibili, Xiaohongshu, Cursor / Notion / Antigravity Electron apps, etc.) plus a chrome.debugger extension auto-loaded into the agent's own Chrome. **Zero LLM tokens at runtime** for any task that maps to a built-in adapter.

**Why it complements browser-harness rather than replacing it:**

| Use case | Right backend |
|---|---|
| Site has a built-in OpenCLI adapter | `OpenCliRun` (zero tokens, deterministic) |
| Site doesn't have one yet, recurring task | `OpenCliBrowse` → `OpenCliAuthor` (one-time author, then free forever) |
| One-off raw exploration | browser-harness `BrowserNavigate / Snapshot / Click / Type / Vision` |
| VPS deployment (no real Chrome avail) | browser-harness only |

**Five tools registered:**
- `OpenCliList` — discover the 100+ adapters (call first)
- `OpenCliRun` — run a deterministic adapter
- `OpenCliBrowse` — live browser ops via the chrome.debugger extension
- `OpenCliAuthor` — crystallize a browse session into a reusable adapter
- `OpenCliInspect` — inspect adapter source / args / status

**Files (`extensions/opencli-bridge/`):**
- `extension/v1.0.6/` — bundled Chrome extension (Apache-2.0, redistributed)
- `plugin.py` — register entry + PATH prepend + extension side-load + HOME-shim
- `dispatcher.py` — `subprocess.Popen(["opencli", ...])` JSON parser
- `tools.py` — 5 `BaseTool` wrappers, all with priority hints in descriptions
- `actions.py` — `OpenCliBridgeActions` for adapter-runner (parallel to `BrowserHarnessActions`)
- `doctor.py` — three-step health check
- `skills/opencli-routing/SKILL.md` — routing decision tree (when to use what)
- `VENDORED.md` — provenance + Apache-2.0 attribution + re-sync checklist

**External deps added:**
- `@jackwener/opencli ^1.0.6` (resolves to 1.7.14) in [package.json](package.json) — npm install pulls it project-local at `node_modules/.bin/opencli`
- The bundled extension ships in the OC repo; user takes no install step

**Side-load mechanism:** `plugin.py` appends the extension path to `AGENT_BROWSER_EXTENSIONS` (additive, comma-separated). agent-browser's launcher passes that through as `--load-extension=<path>` to Chromium. Verified loaded by reading `chrome://extensions` shadow DOM during smoke tests.

**Per-OC-profile state isolation (HOME-shim):** opencli hardcodes `os.homedir() / ".opencli"` for state ([upstream main.js:29](node_modules/@jackwener/opencli/dist/src/main.js)). To avoid clobbering the user's real `~/.opencli/`, `plugin.py:_setup_home_shim()` builds a per-OC-profile shim:

```
<oc_profile_home>/
├── opencli/                   ← REAL state (authored adapters, configs)
└── opencli-shim-home/
    └── .opencli  →  ../opencli  ← symlink the dispatcher's HOME points at
```

dispatcher.py sets `HOME=<oc_profile_home>/opencli-shim-home` per subprocess. Surgical: opencli only uses `os.homedir()` for the state path, so this override has no other side effects. Each `oc -p <name>` gets a fully isolated OpenCLI state tree.

**Smoke test that passed:** `OpenCliList(filter="hackernews")` → returned catalog. `OpenCliRun(site="hackernews", command="top", args={limit: 3})` → returned 3 real top HN stories with rank/score/author/url, zero LLM tokens, JSON output.

**Known caveats:**
- opencli's existing `~/.opencli/clis/` (e.g., user's prior `luma`, `learnx.bak`, `linkedin`, `swiggy` adapters) is NOT migrated automatically into per-OC-profile dirs. User can copy what they want manually. Default behavior is fresh start per OC profile.
- The format flag is `-f json` (NOT `--json`) — dispatcher auto-injects it. Got bit by this once; document.
- OpenCLI doesn't provide an `OPENCLI_HOME` env var — that's why we needed the symlink shim.
- 5 OpenCLI upstream skills (`opencli-adapter-author`, `opencli-autofix`, `opencli-browser`, `opencli-usage`, `smart-search`) were NOT mirrored verbatim because the GH API fetch was sandboxed; we wrote our own concise `opencli-routing` skill that captures the decision tree. Future: fetch + mirror those 5 (Apache-2.0 allows).
- The chrome.debugger extension can't attach to a tab agent-browser is already CDP-controlling (one-debugger-per-tab Chrome rule). Tab partitioning solves it: each path opens its own tabs. The agent picks per task, doesn't try to use both on the same tab.

**LLM tool-selection steering:** purely encoded in tool descriptions (e.g., `OpenCliRun.description` says "PREFERRED for any web data task. Returns clean JSON, ZERO LLM tokens at runtime. ... If this returns 'adapter_not_found', do NOT just fall back to live browsing without crystallizing"). No InjectionEngine wiring needed — descriptions are read every turn and Claude follows priority hints reliably. Optional: post-task reflection hook for stronger nudge could be added later.

---

### 4.3 CC §4 + §10 visibility surface (2026-05-10, schema v18)

**Status:** active default. Closes the `/context` and `/usage` gaps documented in `docs/OC-FROM-CLAUDE-CODE.md` (§4 + §10).

**Schema v18:** additive `sessions.compactions_count INTEGER DEFAULT 0`. AgentLoop's `_record_compaction()` bumps it after each successful `CompactionResult.did_compact` at both proactive and reactive compaction sites. The increment is atomic via `RETURNING` with a graceful pre-3.35 SQLite fallback. Telemetry must never wedge the loop — three-tier error handling (DB swallow → loop swallow → slash empty-state) guarantees that.

**Surface added:**
- `/context` slash — model, used/max tokens, remaining, compaction-trigger threshold, compactions this session, total input tokens. Reads `runtime.custom`.
- `/usage` slash — augmented: compactions row when `session_compactions > 0`. Existing cache row, cost, tokens rows preserved.
- `oc context show <session-id>` — historical session panel from SessionDB.
- `oc context show --current` — render for the most-recent session.
- `oc context list [--limit N]` — overview table: every session with its context % + compaction count.
- `oc usage sessions [--session-id|--model|--provider|--since|--limit]` — SessionDB-backed per-session view (compactions + cost from joined `llm_calls`). Distinct from the existing top-level `oc usage` callback that reads JSONL telemetry.

**Honest cost rendering:** `SessionUsageRow.cost_usd: float | None`. When `llm_calls` lacks pricing data the CLI shows `—`, not `$0.00`.

**Why two commands on `oc context`** (`show` + `list`): Typer auto-promotes Typer apps with a single command, collapsing `oc context show <id>` parse. Registering `list` as a second command suppresses the auto-promote AND provides a useful discovery surface.

**Spec:** `docs/superpowers/specs/2026-05-10-cc-usage-context-visibility-design.md`.

---

### 4.4 Self-evolution closed loop (2026-05-11)

**Status:** active default. Closes the four-island self-evolution gap documented in the 2026-05-11 brutal-honest review (skill-evolution machinery + dreaming v1/v2 + outcome-aware events all existed but were not connected).

**What it does:**

1. **Honcho `on_pre_compress`** — the prior TODO stub is replaced with a real `/v1/context-full` GET (2-attempt retry on 5xx / network). Returns a pinned `## Honcho user-model facts` block injected before compaction so peer-card content survives the summariser.
2. **Honcho turn-completed handler** — the prior log-only stub is replaced with `conclude(observation_mode=inferred)` POST (2-attempt retry on 5xx / network). Every turn's signals land in Honcho's user-model as an inferred behavioral observation. Non-JSON-serializable signal values are repr-coerced before render.
3. **Per-turn trace id** — `opencomputer.observability.trace` exposes a contextvar (`new_trace_id / set_trace_id / get_trace_id / reset_trace_id / trace_scope`). `AgentLoop.run_conversation` opens a scope at turn entry; `record_llm_call` auto-fills `LLMCallEvent.trace_id` from the contextvar when the caller doesn't supply one explicitly.
4. **Langfuse parent span per turn** — `extensions.langfuse.plugin.open_turn_span` is a context manager called by `run_conversation` alongside `trace_scope`. All langfuse generations + tool spans created during the turn nest under one parent span via OTel context propagation. langfuse-inert mode is a no-op.
5. **`SkillReviewDecisionEvent`** — new typed event in `plugin_sdk.ingestion`. Emitted by `oc skills accept / reject / review` on the default bus. Decision vocabulary: `accepted | rejected | edited | deferred`.
6. **`EvolutionOrchestrator`** — `opencomputer/agent/evolution_orchestrator.py`. Subscribes to `skill_review_decision` + `turn_completed`. Maintains a rolling window of 20 decisions. Tunes `confidence_threshold` (skill-evo Stage-2) and `dreaming_v2_score_threshold` + `dreaming_v2_min_recall` on a hysteresis schedule (accept-rate <30% tightens, >80% loosens, dead band in between, min 10 decisions). Persists to `<profile_home>/skills/evolution_tuning.json` atomically with `fcntl.flock` on POSIX.
7. **Gateway lifecycle wiring** — `Gateway._start_evolution_orchestrator` starts the orchestrator at daemon boot, stops at shutdown. CLI-mode users hit `EvolutionOrchestrator.get_or_start_orchestrator` lazy singleton (via `oc skills accept / reject / review` event-emission path) so standalone CLI sessions ALSO drive tuning.
8. **`oc evolution-tuning` CLI** — `status` shows current tuning + decisions observed + last recompute, `tune` forces a manual recompute, `reset --yes` clears to defaults. Aggregate-only (privacy posture mirrors `oc skills evolution status`). Named `evolution-tuning` rather than `evolution` to avoid colliding with the existing trajectory/prompts/skills `oc evolution` namespace (PR-1).
9. **Skill-evolution subscriber** — `_run_pipeline_inner` calls `load_tuning(_home())` and uses the result as the effective `confidence_threshold`, overriding the constructor default. Fall-back to ctor default on tuning-file read failure.
10. **Provenance carries trace_id** — `skill_extractor` captures `get_trace_id()` at extraction time and stores it in `provenance.json`. `oc skills review` reads it back when emitting the decision event so the langfuse `score_trace` callback can post a decision score against the right server-side trace.

**Persisted state:** `<profile_home>/skills/evolution_tuning.json` (schema v1; additive fields permitted, breaking changes bump version → reader falls back to defaults).

**Failure isolation:** every wire degrades gracefully — Honcho down → fall back to none; langfuse inert → no scoring, tuning continues; orchestrator missing → decisions still flow on bus, no tune; bus event handler exception → logged + swallowed, never re-raised into the publisher.

**Spec:** the 2026-05-11 in-chat brutal-honest review and the on-the-fly /brainstorm → /audit-design → /plan → /audit-plan workflow. No standalone spec doc (work was scoped tight enough that one would have been bureaucracy).

---

### 4.5 `oc workspace` — hermes-workspace as a second browser surface (2026-05-12)

**Status:** active default. Sibling to `oc webui` — leaves the existing webui untouched. `oc workspace` launches [hermes-workspace](https://github.com/outsourc-e/hermes-workspace) (MIT, Node SSR React app) pointed at OC's dashboard FastAPI as an OpenAI-compatible chat backend.

**What it adds:**

1. **OpenAI-compat HTTP shim** — `opencomputer/dashboard/routes/openai_compat.py` adds three routes to the existing dashboard FastAPI app (port 9119):
   - `GET /v1/health` — public liveness probe
   - `GET /v1/models` — OpenAI list shape over `cli_model_picker._grouped_models()`, deduped by model id
   - `POST /v1/chat/completions` — Bearer-gated, streaming (SSE) + non-streaming, backed by `AgentLoop.run_conversation`. Stateless per request: the `messages[]` array drives `initial_messages`; the final user turn is `user_message`. Tool calls happen inside the loop but are not surfaced as OpenAI `tool_calls` deltas in v1 — only the terminal text response is streamed back. Body capped at 4 MiB; completion wall-clock cap 10 minutes; backpressure-safe SSE pump (drop deltas + tail marker rather than blocking the model thread).
2. **`oc workspace` CLI** (`opencomputer/cli_workspace.py`):
   - `oc workspace` (bare) / `oc workspace run` — discover hermes-workspace dir, check prereqs (node ≥ 22, pnpm ≥ 9), build if needed, spawn dashboard thread + Node subprocess, health-check both, open browser, block until Ctrl+C.
   - `oc workspace build [--force]` — run `pnpm install` + `pnpm build`.
   - `oc workspace doctor` — print prereq status + discovery + cache state.
3. **Launcher package** (`opencomputer/workspace/`):
   - `discovery.py` — explicit `--workspace-dir` → `$OC_WORKSPACE_DIR` → `<profile>/workspace/` → `~/.opencomputer/workspace/` → `/Users/saksham/Vscode/claude/sources/hermes-workspace/` dev-fallback. Explicit-then-invalid is a HARD ERROR, never silent fallback.
   - `prerequisites.py` — `node --version` + `pnpm --version` with version-major gates and timeout.
   - `builder.py` — cache hit when `dist/server/server.js` + `node_modules/.modules.yaml` are both present AND newer than `package.json`. Detects interrupted installs (presence of `node_modules/` without `.modules.yaml` = "half-baked, reinstall").
   - `launcher.py` — `node server-entry.js` subprocess with enriched env (`HERMES_API_URL`, `HERMES_API_TOKEN`, `PORT`, `HOST`, `NODE_ENV`, `OPENCOMPUTER_HOME`). POSIX: `start_new_session=True` + process-group SIGTERM→5s→SIGKILL on shutdown. Health-check polls `http://host:port/` until non-5xx with exponential backoff capped at 2s.
   - `lifecycle.py` — coordinates: start in-process `DashboardServer` thread, capture `app.state.session_token`, await `/api/health`, then `spawn_workspace`, then optionally `webbrowser.open`. Refuses to start when the dashboard port is in use (token is per-process; reuse needs disk persistence which is a follow-up).

**Env vars:**

| Var | Default | Purpose |
|---|---|---|
| `OC_WORKSPACE_DIR` | (unset; discovery) | Override workspace dir |
| `HERMES_API_URL` | set by launcher | Workspace → gateway URL (chat completions, models) |
| `HERMES_DASHBOARD_URL` | set by launcher | Workspace → dashboard URL (sessions, skills, jobs). In OC's world this points at the same FastAPI as `HERMES_API_URL` (sibling routers on one app). |
| `HERMES_API_TOKEN` | set by launcher | Bearer token for `/v1/*` |
| `CLAUDE_DASHBOARD_TOKEN` / `CLAUDE_API_TOKEN` | set by launcher | Mirror of `HERMES_API_TOKEN` for the workspace's gateway-capabilities layer (per upstream #124 migration) — without this it falls back to a deprecated HTML-scrape token flow. |
| `/health` route (no `/v1/` prefix) | added 2026-05-12 | Gateway-shape liveness alias the workspace's capabilities probe expects |
| (set into subprocess) `HOST`, `PORT`, `NODE_ENV`, `OPENCOMPUTER_HOME` | as appropriate | Workspace runtime |

**Failure modes (all surface loudly):**
- node / pnpm missing or too old → `oc workspace doctor` shows MISSING; `run` exits 1 with install link
- Workspace dir not found → list every searched path; suggest `git clone` target
- Dashboard port in use → exit 1 with `--dashboard-port` hint (no token-discovery for shared dashboards in v1)
- Node exits before health-check → `LaunchFailed` with the captured exit code
- AgentLoop raises mid-stream → in-band SSE error chunk + `data: [DONE]`; HTTP stays 200 because the stream is already open
- 4 MiB+ body → 413 OpenAI error envelope (`HTTP_413_CONTENT_TOO_LARGE` w/ legacy `HTTP_413_REQUEST_ENTITY_TOO_LARGE` fallback)
- Empty / no-user-message / malformed JSON / missing `messages` → 400 OpenAI error envelope with structured `code`

**Honest scope limits (documented in CLI startup banner):**
- Sessions / Skills / MCP / Conductor / Swarm tabs in the workspace show "Not Available" — those endpoints (`/api/sessions/...`, `/api/skills/...`, `/api/conductor/...`) are hermes-agent-shape, OC exposes its own `/api/v1/...` shape. Mapping is a future PR.
- Tool-call rendering: workspace will not show OC's `Edit`/`Bash`/etc. tool calls — only the terminal text response is streamed back. Translating OC tool blocks to OpenAI `tool_calls` deltas is a future PR.
- Per-request AgentLoop construction (~1s cold) — high-concurrency users will feel it. Per-profile loop cache is a future PR (see comment in `_run_agent_completion`).

**Spec:** `docs/superpowers/specs/2026-05-12-oc-workspace-hermes-design.md` + workflow notes at `docs/superpowers/specs/2026-05-12-oc-workspace-hermes-workflow-notes.md`.

**Tests:** `tests/test_workspace_discovery.py`, `test_workspace_prerequisites.py`, `test_workspace_builder.py`, `test_workspace_launcher.py`, `test_cli_workspace.py`, `test_dashboard_openai_compat.py`. 72 tests; full green.

---

## 5. What's NEXT — single source of truth

> **This section is the authoritative phase map.** The omnibus plan `~/.claude/plans/2026-04-23-honcho-ecosystem-omnibus.md` drove Sub-projects A–D to completion; two older plans (`delightful-sauteeing-sutherland.md`, `phase-12-ultraplan-spec.md`) are superseded — do not use them.

### Current stance — v1.0 candidate, dogfood gate next

**All v1.0 ship-gate sub-projects are merged on `main`** (tip: `5c62a12`):

- Sub-project A — Honcho-as-default memory ✅ (PR #20)
- Sub-project B — `opencomputer plugin new` scaffolder ✅ (PR #21)
- Sub-project C — Profile parity with Hermes ✅ (PR #24)
- Sub-project D — Coding-harness completeness ✅ (PRs #22, #23, #27)
- Sub-project E — Demand-driven plugin activation ✅ (PR #26)

Next concrete action: tag v1.0 + PyPI release (see `RELEASE.md`).

### 🛑 Dogfood gate — 2 weeks before v1.1 scope

Before expanding beyond v1.0, use OpenComputer daily for 2 weeks. Feature priorities must come from actual usage gaps, not guesses. This gate is load-bearing — don't skip.

### Immediately actionable (Tier 1)

- **Tag v1.0 + PyPI release** (~1 hr; runbook in `RELEASE.md`; human-attended — PyPI publish uses OIDC tied to maintainer's GitHub identity, requires explicit sign-off).
- **Phase 10d — publish example third-party plugin repo to PyPI** (1-2 days). Now unblocked by Sub-project B — the scaffolder is what the example would demonstrate end-to-end.

### Dogfood-gated (Tier 2 — park until real demand signals)

- **Phase 12m — MCP install-from-catalog + reconnect/health.** (Renamed from the older "Phase 12b" label to avoid collision with the `phase-12b*` branch-naming convention used for Sub-project D work, which is distinct and already merged.)
- Phase 12c.1 — first 5 channel adapters (Slack, Matrix, Email, Webhook, OpenAI-compat API).
- Phase 12c.2–4 — 15 more channel adapters.
- Phase 12d.3–6 — memory-vector, memory-wiki, local-providers, media-tools plugin ports.
- Phase 12e — coding-harness dedup audit.
- Phase 14.F/G/H/K — per-profile credential isolation, templates, sharing, profile-aware MCP.
- Phase 15.A — `opencomputer session resume` CLI wiring. Checkpoint table shipped; CLI surface pending.

### Parked by design (Tier 3 — big scope, don't start without explicit go-ahead)

- **Sub-project F — User Intelligence System.** 10-phase roadmap at `~/.claude/plans/there-are-many-pending-tranquil-fern.md` (F1 consent layer → F10 plural-representation ensemble). Explicitly parked until post-v1.0.

### Latent tech debt (Tier 4 — cheap cleanup when convenient)

- `profile.yaml` write lacks `flock` — concurrent plugin-enable calls silently last-write-wins (~1 hr).
- Strict `load_profile_config` vs lenient plugin enable/disable YAML handlers — two parse paths to unify (~1-2 hr).
- AgentCache utility shipped in Phase 12a as a class + tests but never wired into production caller (~half day if pursued).
- Per-profile `.env` loading for Phase 14.F credential isolation (~1 day).
- E7 — keyword-match demand detection on `UserPromptSubmit` hook (natural-language intent scanner so demand signals fire even when the model never calls the missing tool; ~1-2 days).

### Won't do (Tier 5 — parked forever)

Canvas rendering, native mobile apps, voice wake-word, Atropos RL, trajectory compression, 6 remote terminal backends, skills marketplace, full i18n. Reopen only if a concrete use case appears.

---

### Non-obvious infra notes (add to your mental model)

**Plugin registration is Python-declarative, not YAML-based.** There are no `manifest.yaml` or `manifest.toml` files in `extensions/`. Each plugin's metadata (name, description, kind, tool set) is declared via a `register(api)` function in its `plugin.py`, typically constructing a `PluginManifest` from `plugin_sdk`. Don't hunt for YAML manifests — they don't exist.

**Manifest schema v4 fields (Sub-project G openclaw-parity, 2026-05-03):**

- `min_host_version` (string) — minimum `opencomputer.__version__` required; empty = no check. Validated as PEP 440 / semver / calver. Enforced at `loader.load_plugin` BEFORE entry-module import; mismatch → log + skip with `PluginIncompatibleError`. `extensions/anthropic-provider/plugin.json` uses this as the canonical example.
- `activation` (object) — manifest-declared triggers: `on_providers`, `on_channels`, `on_commands`, `on_tools`, `on_models`. Read by `opencomputer.plugins.activation_planner.plan_activations`. Falls back to legacy `tool_names` inference (Sub-project E, PR #26) when absent. Existing Sub-project E demand-driven path remains unchanged.
- `setup.providers[].auth_choices` (array) — rich auth UI metadata: per-method `label`, `cli_flag`, `option_key`, `group`, `onboarding_priority`. Falls back to legacy `auth_methods: list[str]` interpretation when empty.
- `plugin.json` is now JSON5-tolerant (comments, trailing commas) via two-tier parse: `json.loads` first, `json5.loads` only on JSONDecodeError. Plain JSON manifests pay zero overhead.
- 256KB cap on `plugin.json` size at discovery (`MAX_MANIFEST_BYTES`); pathological files skipped with a warning.
- New: `opencomputer plugin inspect <id>` — compares manifest claims to actual `LoadedPlugin.registrations` post-load. Status `valid` / `drift`.
- New: `plugin_sdk.SecretRef` + `SecretResolver` — typed wire primitive whose `model_dump()` never includes the value. Adoption is opportunistic (new wire methods only).
- New: `opencomputer.gateway.error_codes.ErrorCode` (StrEnum) + `WireResponse.code: str | None` — typed error categories that wire clients can match on. Old clients ignore.
- New: `tests/test_plugin_extension_boundary.py` — frozen-inventory test that fails on any NEW `from opencomputer.*` import inside `extensions/*.py`. Existing 26 violators frozen at `tests/fixtures/plugin_extension_import_boundary_inventory.json`. Cleanup is a per-extension follow-up.

All v4 fields are optional; v3 manifests parse unchanged.

**Schema-name uniqueness is the collision guard for tool names.** If two plugins register tools with the same `schema().name`, `ToolRegistry` raises `ValueError` at load. Tool names are PascalCase by convention (Edit, MultiEdit, Read, TodoWrite, etc.) — the SDK boundary test keeps this honest.

**Settings-based hooks — declare shell hooks without writing a plugin (III.6).** The top-level `hooks:` key in `~/.opencomputer/<profile>/config.yaml` accepts the same event-keyed shape Claude Code uses in `.claude/settings.json`:

```yaml
hooks:
  PreToolUse:
    - matcher: "Edit|Write|MultiEdit"
      command: "python3 /path/to/linter.py"
      timeout_seconds: 10
  Stop:
    - command: "bash /path/to/cleanup.sh"
```

**Wire protocol** (augmented 2026-05-08 — Hermes Doc-2 G3/G4):

* **stdout JSON (preferred)** — when the script's stdout parses as a JSON object, recognised keys take precedence over the exit code:
  - `{"action": "block", "message": "..."}` → block (Hermes canonical)
  - `{"decision": "block", "reason": "..."}` → block (Claude Code)
  - `{"action": "approve" \| "allow"}` or `{"decision": "approve"}` → pass
  - `{"context": "..."}` → on PRE_LLM_CALL only, append text to user message; ignored on other events
  - `{}` or unrecognised keys → pass
  - malformed JSON → fall back to exit-code path
* **Exit-code (fallback)** — when stdout is empty or non-JSON: `0` → pass, `2` → block with stderr as reason, anything else → fail-open warn+pass.
* **Timeouts and crashes** — fail-open. A wedged hook must never wedge the loop.

Env vars: `OPENCOMPUTER_EVENT`, `OPENCOMPUTER_TOOL_NAME`, `OPENCOMPUTER_SESSION_ID`, `OPENCOMPUTER_PROFILE_HOME`, plus `CLAUDE_PLUGIN_ROOT` aliased to profile home so Claude Code hook scripts drop in unchanged. A JSON blob carrying the `HookContext` is piped to the command's stdin. See `sources/claude-code/plugins/plugin-dev/skills/hook-development/SKILL.md` for the inspiration; settings-declared hooks coexist with (and fire AFTER) plugin-declared ones.

**`oc hooks` CLI** (2026-05-08 — Hermes Doc-2 G1/G2): `oc hooks list` shows registration + last-fire metadata, `oc hooks test EVENT --execute [--for-tool NAME]` actually fires synthetic events through the engine (not just dry-run), `oc hooks doctor [--json]` surfaces health diagnostics for gateway file-discovery hooks (HOOK.yaml validity, handler import) plus settings-hook executable resolution plus recent fire activity.

**Bundled settings variants (III.3).** Three starter `config.yaml` templates live under `opencomputer/settings_variants/` — `lax.yaml` (permissive dev posture, no hooks), `strict.yaml` (tightened loop budget + PreToolUse audit hook), and `sandbox.yaml` (placeholder Bash-sandbox hook; full wrapper lands with F3). Mirrors Claude Code's `sources/claude-code/examples/settings/README.md` examples. Discover and initialize from the CLI:

```bash
opencomputer config variants                           # list the three variants + descriptions
opencomputer config init --variant strict              # copy strict.yaml → ~/.opencomputer/<profile>/config.yaml
opencomputer config init --variant lax --force         # overwrite an existing config.yaml
```

The init command verifies the copied file re-parses via `load_config()` before confirming success; a bad variant rolls back (or restores the previous file on `--force`) so the user never ends up with a half-written `config.yaml`. Variants are starting points — edit the copied file freely after init; they integrate with the III.6 settings-hooks surface above.

---

## 6. How to run / develop / test

### Local setup

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
source .venv/bin/activate   # venv uses Python 3.13 (anaconda)
```

### Run the CLI

```bash
# Prereqs — one of:
export ANTHROPIC_API_KEY=sk-ant-...              # native Anthropic
# OR
export ANTHROPIC_BASE_URL=https://claude-router.vercel.app
export ANTHROPIC_AUTH_MODE=bearer
export ANTHROPIC_API_KEY=<router-proxy-key>      # proxy mode
# OR
export OPENAI_API_KEY=sk-...                     # OpenAI

opencomputer               # chat
opencomputer --plan        # plan mode (Edit/Write/Bash refused)
opencomputer gateway       # daemon for Telegram/Discord
opencomputer wire          # WebSocket API at ws://127.0.0.1:18789
opencomputer plugins       # list 7 installed plugins
opencomputer skills        # list skills
opencomputer doctor        # health check (multi-layer)
opencomputer config show   # dump config

# Memory subcommands (`opencomputer memory --help` for the full set)
opencomputer memory audit            # per-paragraph inspection of MEMORY.md (PR #588)
opencomputer memory audit --user     # same for USER.md
opencomputer memory audit --all      # both files
opencomputer memory audit --interactive  # walk + prompt keep/delete/replace/skip
opencomputer memory show [--user]    # cat the file
opencomputer memory edit [--user]    # open in $EDITOR
opencomputer memory prune [--user]   # clear file (.bak preserved)
```

### Test / lint

```bash
pytest tests/                                          # all ~600 tests
pytest tests/test_phase6b.py -v                        # one file
ruff check opencomputer/ plugin_sdk/ extensions/ tests/  # lint
```

### Cut a release (when ready)

See `RELEASE.md` — basically bump version in two places, tag `vX.Y.Z`, push. CI handles PyPI.

---

## 7. Non-obvious gotchas (burned-in lessons)

1. **Plugin module-cache collisions.** When multiple plugins share sibling file names (`plugin.py`, `provider.py`), Python's `sys.modules` returns the first-loaded one for all imports. `plugins/loader.py` solves this: synthetic unique module names via `importlib.util.spec_from_file_location` + `_clear_plugin_local_cache()` between plugin loads. Tests use the same pattern (`importlib.util.spec_from_file_location` with unique names).

2. **Claude Router proxy rejects x-api-key.** Some Anthropic proxies forward `x-api-key` unchanged to upstream Anthropic, which then rejects the proxy_key. `extensions/anthropic-provider/provider.py` supports `ANTHROPIC_AUTH_MODE=bearer` which uses `Authorization: Bearer` AND strips `x-api-key` via an httpx event hook before the request goes out.

3. **Compaction MUST preserve `tool_use`/`tool_result` pairs atomically.** Splitting them causes Anthropic's API to 400. `CompactionEngine._safe_split_index` walks back from the naive split point until it lands outside of any `tool_use`/`tool_result` pair.

4. **`DelegateTool._factory` needs `staticmethod` wrap.** Lambdas stored as class attributes get bound to `self` when accessed via instances. `set_factory` uses `cls._factory = staticmethod(factory)` to prevent this.

5. **asyncio subprocesses can't cross event loops.** A process started in one `asyncio.run()` can't be awaited in another. Background-process tests must do spawn + check + kill in one `asyncio.run()` call.

6. **The plugin SDK boundary is enforced by a test.** `tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer` scans `plugin_sdk/*.py` for `from opencomputer` imports and fails if any exist. Do not bypass this — it's how the contract stays honest.

7. **HookContext.runtime is optional for backwards compat.** Hooks written before Phase 6a don't pass it. New hooks should read modes through `effective_permission_mode(ctx.runtime)` (exported from `plugin_sdk`) rather than `ctx.runtime.plan_mode` / `ctx.runtime.yolo_mode` directly — the helper accounts for slash-command toggles living in `runtime.custom`.

8. **Typer auto-promotes single-command apps.** A `typer.Typer(name="X")` with exactly one `@app.command(...)` collapses to a no-subcommand CLI, so `runner.invoke(app, ["show", arg])` misparses (the literal `"show"` becomes the first arg). Always register a second command — even a useful listing surface — to suppress the auto-promote. See `opencomputer/cli_context.py` (`show` + `list`) for the pattern.

9. **`AgentLoop._runtime` is aliased to the module-shared `DEFAULT_RUNTIME_CONTEXT` at `__init__` time.** Writes to `_runtime.custom` from methods called BEFORE `run_conversation` therefore leak across `AgentLoop` instances in the same process (most visible in tests). `run_conversation` rebinds `_runtime` per call so production paths are fine, but unit tests that exercise loop helpers directly must re-bind: `loop._runtime = RuntimeContext()`. See `tests/test_loop_compaction_increments_counter.py::_fresh_loop` for the pattern.

10. **Counter telemetry must never break the loop.** Anything bumping a per-session counter (compactions, future events) follows the three-tier swallow: `SessionDB.<method>` catches `sqlite3.Error` + returns sentinel; the `AgentLoop` helper catches any broad exception + logs WARNING; the slash / CLI renderer falls back to empty-state. A wedged counter must never wedge the agent. See `_record_compaction` for the canonical pattern.

---

## 8. User preferences (learned across this project)

- **Action over confirmation.** Saksham asks Claude to just do things. Don't ask "should I...?" — do what can be done, then report.
- **Per-phase workflow is a hard rule:** after each phase, review → `git push` → next phase. Never chain phases without the commit boundary.
- **Concise communication.** Short responses. Direct. Don't pad.
- **Stock market data must be live.** Not relevant to OpenComputer, but if stocks come up in a future session: use MCP servers (investor-agent, stockflow) + fresh web search. Never stale cached data.
- **Plugins + skills + MCPs should be checked before answering.** If a relevant tool exists, use it rather than guessing.

---

## 9. If you need to dig deeper

- **Active plan (2026-04-23 onward):** `~/.claude/plans/2026-04-23-honcho-ecosystem-omnibus.md` — current sub-projects A/B/C/D.
- **Superseded historical plans (kept for context):**
  - `~/.claude/plans/delightful-sauteeing-sutherland.md` — original master roadmap (Phase 0 through Phase 13). Superseded 2026-04-23.
  - `~/.claude/plans/phase-12-ultraplan-spec.md` — Phase 12 detail spec. Superseded 2026-04-23.
- **Reference implementations cloned locally at `../sources/`:**
  - `/Users/saksham/Vscode/claude/sources/claude-code/` (plugin shapes)
  - `/Users/saksham/Vscode/claude/sources/hermes-agent/` (Python patterns, loop, channels)
  - `/Users/saksham/Vscode/claude/sources/openclaw/` (plugin SDK boundary, discovery)
  - `/Users/saksham/Vscode/claude/sources/kimi-cli/` (dynamic injection, compaction, wire)
- **Per-repo extraction notes:** `OpenComputer/docs/refs/<repo-name>/` — take notes
  about each reference project here as you study it.
- **Storage map (every SQLite DB, table, owner module):** `OpenComputer/docs/databases.md` — single canonical reference for `sessions.db` + 7 sub-DBs.
- **GitHub:** https://github.com/sakshamzip2-sys/opencomputer

---

## 10. One-line session restart prompt

If you're a fresh Claude Code session, the user typically opens with something like:

> "Continue OpenComputer from where we left off. Read CLAUDE.md §5 first, then pick up the next task in the omnibus plan (Sub-project A / B / C / D)."

That's enough context to start coding.
