# OpenComputer — Session Context for Claude Code

This file is auto-loaded at session start. It is the **single comprehensive brief** a new Claude session needs to resume work on OpenComputer without re-explaining anything.

Last updated: 2026-04-23 (Phase 12a merged)

---

## 1. Project elevator pitch

**OpenComputer** is a personal AI agent framework, written in Python 3.12+, that synthesizes the best ideas from four reference projects into one cohesive system:

| Reference | What we took |
|---|---|
| [Claude Code](https://github.com/anthropics/claude-code) | Plugin primitives (commands/skills/agents/hooks/MCP), lifecycle events, tool shapes (Edit, MultiEdit, TodoWrite) |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Python core patterns, three-pillar memory (declarative + procedural + episodic), agent loop shape, channel adapter pattern |
| [OpenClaw](https://github.com/openclaw/openclaw) | Plugin-first architecture, strict SDK boundary, manifest-first two-phase discovery (scan cheap metadata, activate lazily), typed wire protocol |
| [Kimi CLI](https://github.com/MoonshotAI/kimi-cli) | Dynamic injection providers for cross-cutting modes, fire-and-forget hooks, deferred MCP loading, StepOutcome abstraction, Jinja2 prompts |

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
├── tests/                           ← ~600 tests, all passing (40 test files)
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

All committed + pushed to `main`. Current main sha: `1c08508` (2026-04-23).

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

**Test count:** ~600 passing across 40 test files.

**Bundled extensions (7):** telegram, discord, anthropic-provider, openai-provider, coding-harness, dev-tools, memory-honcho.

---

## 5. What's NEXT — single source of truth

> **This section is the authoritative phase map.** The omnibus plan file at `~/.claude/plans/2026-04-23-honcho-ecosystem-omnibus.md` is a user-scoped draft (lives on the maintainer's laptop, not in the repo) that expands each sub-project into TDD-granular task lists — use it if you have local access, otherwise this section alone is enough to orient. The two older plans (`delightful-sauteeing-sutherland.md`, `phase-12-ultraplan-spec.md`, both in `~/.claude/plans/`) are marked SUPERSEDED — do not use them.

### Current stance — what blocks v1.0

The v1.0 ship-gate is:

- **Sub-project A — Honcho as default memory** (4–6 days). Make the Honcho overlay the default memory provider, not opt-in. Baseline SQLite still works without Docker, but a fresh install onboards into Honcho by default. Details in the omnibus plan.
- **Sub-project B — `opencomputer plugin new` scaffolder** (4–6 days). This is what Phase 10c was. Renamed to Sub-project B in the omnibus. Ships a CLI that generates a working plugin skeleton + `docs/plugin-authors.md` + `docs/sdk-reference.md`. Ship-blocker for v1.0.
- **Sub-project D — Coding-harness completeness** (5–7 days). Rebase + merge the `feat/coding-harness-v2` branch (11 commits of Phase 6d–6f work), then land ExitPlanMode + cheap-route + 3 hooks + the full slash-command router.

**After v1.0 tag → Sub-project C — Profile completeness parity with Hermes** (2–4 days). Per-profile `home/`, wrapper scripts, `SOUL.md`. Lands *after* Sub-project A so profile-aware memory is already in place.

### 🛑 Dogfood gate (unchanged)

Before expanding beyond v1.0, use OpenComputer daily for 2 weeks so feature priorities come from actual gaps, not guesses. This gate is load-bearing — don't skip. Formerly framed as "gate after Phase 10"; still stands, just after A+B+D land.

### Pending work (reorganized under omnibus sub-projects)

Everything below is still pending on `main`. The old phase numbers are kept here for traceability only — the active grouping is the A/B/C/D above.

- **Phase 10c → now Sub-project B.** Plugin scaffolder + author docs.
- **Phase 10d — example third-party plugin repo.** Deferred until Sub-project B ships (the scaffolder is what the example would demonstrate).
- **Phase 12b — MCP install-from-catalog + reconnect/health.** Pending; downstream of Sub-project A settling the config layout.
- **Phase 12c.1 — first 5 channel adapters** (Slack, Matrix, Email, Webhook, OpenAI-compat API). Dogfood-gated.
- **Phase 12c.2–4 — 15 more channels.** Dogfood-gated.
- **Phase 12d.3–6 — memory-vector, memory-wiki, local-providers, media-tools plugin ports.** Dogfood-gated.
- **Phase 12e — coding-harness dedup audit.** Rolled into Sub-project D (the rebase naturally exposes dedup work).
- **Phase 14.F/G/H/K — per-profile credential isolation, templates, sharing, profile-aware MCP.** Deferred; Sub-project C scope does not include these.
- **Phase 15.A — `opencomputer session resume` CLI wiring.** Checkpoint table already shipped; CLI surface still pending.

### WON'T DO (explicitly parked)

Canvas rendering, native mobile apps, voice wake-word, Atropos RL, trajectory compression, 6 remote terminal backends, skills marketplace, full i18n. Re-open only if a concrete use case appears.

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
opencomputer doctor        # health check
opencomputer config show   # dump config
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

7. **HookContext.runtime is optional for backwards compat.** Hooks written before Phase 6a don't pass it. New hooks should use `ctx.runtime.plan_mode` etc.

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
- **GitHub:** https://github.com/sakshamzip2-sys/opencomputer

---

## 10. One-line session restart prompt

If you're a fresh Claude Code session, the user typically opens with something like:

> "Continue OpenComputer from where we left off. Read CLAUDE.md §5 first, then pick up the next task in the omnibus plan (Sub-project A / B / C / D)."

That's enough context to start coding.
