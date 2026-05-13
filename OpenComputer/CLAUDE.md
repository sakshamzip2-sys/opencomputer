# OpenComputer — Session Context for Claude Code

Single-file brief a fresh Claude session needs to resume work on OpenComputer.

Last updated: 2026-05-12. Main tip: `0d5bb3e7`. Most recent merged work: PR #601 (compaction `enable_probe` fix), PR #602 (banner pegasus), PR #603 (banner skill-category rendering).

---

## 1. Project elevator pitch

**OpenComputer** is a personal AI agent framework in Python 3.12+ that synthesizes four reference projects:

| Reference | What we took |
|---|---|
| [Claude Code](https://github.com/anthropics/claude-code) | Plugin primitives (commands/skills/agents/hooks/MCP), lifecycle events, tool shapes (Edit, MultiEdit, TodoWrite) |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Python core patterns, three-pillar memory (declarative + procedural + episodic), agent-loop shape, channel adapter pattern, Jinja2 prompts, BrowserHarness |
| [OpenClaw](https://github.com/openclaw/openclaw) | Plugin-first architecture, strict SDK boundary, manifest-first two-phase discovery, typed wire protocol |
| [Kimi CLI](https://github.com/MoonshotAI/kimi-cli) | Dynamic injection providers, fire-and-forget hooks, deferred MCP loading, StepOutcome abstraction |

Plus [hermes-workspace](https://github.com/outsourc-e/hermes-workspace) — the optional alternative web UI bound via `oc workspace`.

**Positioning:** "Same agent, same memory. Install the coding-harness plugin → it's a coding agent. Don't install → it's a chat agent." Runs from CLI, Telegram, Discord, Slack, Matrix, web UI, and any WebSocket client (TUI, IDE).

- You are **Saksham** (GitHub: `sakshamzip2-sys`)
- Repo: `https://github.com/sakshamzip2-sys/opencomputer` (PUBLIC)
- Authored on macOS (darwin), zsh

---

## 2. Repository layout

```
/Users/saksham/Vscode/claude/
├── .git/                            ← parent repo — GitHub sakshamzip2-sys/opencomputer
├── .github/workflows/               ← test.yml, lint.yml, release.yml (OIDC PyPI)
├── OpenComputer/                    ← THE PROJECT — cd here for code work
└── sources/                         ← reference repos, gitignored (claude-code, hermes-agent, openclaw, kimi-cli, hermes-workspace)
```

### OpenComputer/ structure

```
OpenComputer/
├── pyproject.toml                   ← hatchling, ruff, pytest. Single entry point: `oc = opencomputer.cli:main`
├── CLAUDE.md  AGENTS.md  RELEASE.md  CHANGELOG.md  README.md
│
├── opencomputer/                    ← CORE PACKAGE
│   ├── cli.py                       ← Typer root
│   ├── cli_*.py                     ← 67 subcommand modules. Truth: `ls cli_*.py | sed 's|cli_||;s|\.py||'`
│   ├── agent/                       ← loop / state (SessionDB+FTS5) / memory / compaction (812 LOC) /
│   │                                  injection / evolution_orchestrator / prompt_builder
│   ├── acp/  agents/  awareness/    ← ACP protocol, subagent registry, layered awareness
│   ├── auth/  channels/             ← OAuth helpers, generic channel base
│   ├── tools/                       ← built-in tools + registry (PascalCase tool names)
│   ├── gateway/                     ← daemon + dispatch + wire (WS JSON-RPC at 18789)
│   ├── dashboard/                   ← FastAPI (REST + OpenAI-compat `/v1/*` + Hermes-shape `/api/*` aliases)
│   ├── ui-tui/dist/                 ← TUI build artifact (gitignored, symlinked across worktrees)
│   ├── workspace/                   ← hermes-workspace launcher: discovery + prerequisites + builder + lifecycle
│   ├── hooks/                       ← event dispatcher + fire-and-forget runner
│   ├── plugins/                     ← loader, registry, discovery (NOT the plugins themselves)
│   ├── mcp/                         ← MCPManager + tools + catalog
│   ├── observability/               ← trace contextvars (langfuse plumbing)
│   ├── service/                     ← systemd unit template
│   └── skills/                      ← bundled skills
│
├── plugin_sdk/                      ← PUBLIC CONTRACT. Plugins import from HERE ONLY. Test-enforced.
│   ├── core.py  tool_contract.py  provider_contract.py  channel_contract.py
│   └── hooks.py  injection.py  runtime_context.py  ingestion.py
│
├── extensions/                      ← 86 bundled plugins. `ls extensions/` for the live truth. Categories:
│   │   Channels: telegram, discord, slack, matrix, email, imessage, irc, dingtalk, feishu, gmail, …
│   │   Providers (28+): anthropic, openai, openrouter, gemini, gemini-oauth, azure-foundry, aws-bedrock,
│   │           groq, cerebras, deepseek, deepinfra, llama-cpp-server, kimi, kimi-china, codex,
│   │           copilot-acp, dashscope, huggingface, arcee, jan, kilo, alibaba-coding-plan, …
│   │   Tools: coding-harness, dev-tools, browser-harness (DEFAULT), opencli-bridge, adapter-runner,
│   │          browser-recipes, ambient-sensors, voice, homeassistant, api-server, …
│   │   Memory + observability: memory-honcho, skill-evolution, langfuse
│   │   Legacy (kept dormant): browser-control (typed-error fallback only)
│
├── tests/                           ← ~1,200 test files, ~15k tests passing
├── docs/                            ← see §9 — rich tree (specs, parity docs, runbooks, refs)
└── scripts/                         ← bootstrap_worktree.sh, refresh_extension_boundary_inventory.py, …
```

### Profile state — `~/.opencomputer/<profile>/`

A live OC profile is a fat directory with ~70 entries. The ones you'll touch often:

```
~/.opencomputer/<profile>/
├── config.yaml                      ← canonical profile config (model, loop, memory, mcp, hooks)
├── .env                             ← profile-scoped credentials
├── MEMORY.md  USER.md  DREAMS.md  SOUL.md   ← declarative memory + identity + dreaming-v2 candidates
├── sessions.db (+ -shm/-wal)        ← SessionDB (SQLite + FTS5) — all chat history
├── audit.db                         ← F1 immutable HMAC-chained audit log
├── cron.db                          ← scheduled jobs
├── kanban.db                        ← OpenClaw kanban port
├── .context_window_cache.json       ← current-branch territory: cached probe results
├── feature_flags.json  cost_guard.json  persona_priors.json  learning_moments.json
├── browser-profile/                 ← agent-browser persistent Chromium user-data-dir
├── opencli/  opencli-shim-home/     ← opencli adapter state + HOME-shim symlink target
├── skills/                          ← evolution-staged + accepted skills; evolution_tuning.json
├── agents/  ambient/  audit/  cron/  evolution/  gateway/  hook_history.jsonl
├── kanban/  langfuse/  locks/  logs/  memories/  pairing/  presets/  plugins/
├── profile_bootstrap/  profiles/  rate_limits/  rules/  secrets/  sessions/
├── tool_result_storage/  user_model/  webui/  wire.log  wire.pid
└── home/                            ← Hermes-style wrappers + soul (sub-project C)
```

`oc -p <name>` switches profiles — every path above re-roots. Per-profile credentials, plugins, memory, browser cookies.

---

## 3. Architecture in one diagram

```
                            user
                              │
       ┌──────────────────────┼─────────────────────────────────┐
       │                      │                                 │
       ▼                      ▼                                 ▼
    oc chat              oc gateway                  oc webui / oc workspace
   (streaming           (daemon: Telegram /                (Node SSR + FastAPI
    CLI tokens)          Discord / Slack / Matrix /         dashboard backend
                         IRC / Email / iMessage / …)        on port 9119)
       │                      │                                 │
       │                      │      ┌─── oc wire (WS JSON-RPC 18789, TUI/IDE clients) ──┐
       │                      │      │                                                   │
       └──────────────────────┼──────┴───────────────────────────┘                       │
                              │                                                          │
                              ▼                                                          ▼
                        ╔═══════════╗                                            (same AgentLoop)
                        ║ AgentLoop ║  run_conversation(user_msg, runtime)
                        ╠═══════════╣
                        ║ • inject  ║  plan / yolo / skill modes via InjectionEngine
                        ║ • compact ║  auto-summarize old turns when context full
                        ║ • call    ║  provider.complete() or stream_complete()
                        ║ • dispatch║  tool calls in parallel (safety + consent checks)
                        ║ • hooks   ║  25+ lifecycle events; PreToolUse can block
                        ║ • loop    ║  until model stops calling tools
                        ╚═══════════╝
                              │
                              ▼
                ┌───────────────────────────┐
                │  plugin_sdk/ (PUBLIC)     │   stable contract
                └───────────────────────────┘
                              ▲
                              │  (plugins import from here ONLY)
              ┌───────────┬───┴────────────┬──────────────┬──────────────┐
              ▼           ▼                ▼              ▼              ▼
          channels    providers       tools          memory        observability
          (~10)       (~28)           (coding-       + skills      (langfuse)
                                       harness,      evolution
                                       browser,
                                       opencli,
                                       voice, …)
```

**The rule:** plugins never import from `opencomputer/*`. Only from `plugin_sdk/*`. Enforced by `tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer` (asserts `plugin_sdk/` clean) and `tests/test_plugin_extension_boundary.py` (asserts `extensions/` doesn't grow new violations — 26 existing violators frozen in `tests/fixtures/plugin_extension_import_boundary_inventory.json` as the cleanup floor).

---

## 4. Where the work is

**This file does NOT track shipped work.** `~/.claude/projects/-Users-saksham-Vscode-claude/memory/MEMORY.md` (auto-loaded) carries chronological ship history with PR# + date + condensed scope — that's the truth. `git log` is authoritative for code.

**Active work (current branch):** `feat/oc-compaction-fix-2026-05-12` is fixing a bug in compaction where the context-window cache and OpenRouter catalog were probing even when the caller passed `enable_probe=False`. See `compaction.py:189–297` for the `enable_probe` semantics and `docs/context-window-deep-dive.md` (515 LOC) for the full design.

**Roadmap pointers:**

- **v1.0 candidate shipped.** Tag + PyPI publish is human-attended (OIDC tied to maintainer identity). See `RELEASE.md`.
- **Sub-project F (User Intelligence System).** F1 (consent + immutable audit) and F2/F4/F5 + 3.E/3.F/3.G shipped. F6+ parked at `~/.claude/plans/there-are-many-pending-tranquil-fern.md`.
- **OpenClaw parity.** 8/20 shipped per `oc parity-doctor run`. Tracker: `docs/openclaw-parity-2026-05-10.md` + `docs/OC-FROM-OPENCLAW.md`.
- **Hermes parity.** Multi-wave; grep `MEMORY.md` for "hermes" — 16+ entries through the v2 honest-audit closures. Doc parity not formally tracked anymore (most of the surface already lifted).

For any specific feature, grep `MEMORY.md` by name / PR# — entries are condensed and chronological.

---

## 5. How to run / develop / test

### Local setup

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
source .venv/bin/activate   # venv uses Python 3.13 (anaconda)
```

### Worktree / merge refresh — non-negotiable

After `git worktree add` OR any merge into the active worktree:

```bash
pip install -e . --no-cache-dir --no-deps   # refresh editable shim
hash -r                                      # zsh: clear command cache (NOT `source ~/.zshrc`)
./scripts/bootstrap_worktree.sh              # symlinks ui-tui/dist/ + dashboard/static/spa/ into fresh worktrees
```

Stale `oc` binary after a merge is the single most common parallel-session failure mode.

### Credentials — one of:

```bash
export ANTHROPIC_API_KEY=sk-ant-...                                                   # native Anthropic
export ANTHROPIC_BASE_URL=https://claude-router.vercel.app ANTHROPIC_AUTH_MODE=bearer  # proxy mode
export OPENAI_API_KEY=sk-...                                                          # OpenAI
# OR `oc auth login <provider>` — token cached per-profile in <profile>/.env
```

### CLI surface

There are **67 subcommand modules** (`ls opencomputer/cli_*.py`). The frequently-used ones:

```bash
oc chat                                # streaming CLI (-c resume last, -q quiet)
oc --plan                              # plan mode (Edit/Write/Bash refused)
oc gateway                             # channel daemon
oc wire                                # WebSocket JSON-RPC at ws://127.0.0.1:18789
oc webui                               # built-in React webui
oc workspace                           # hermes-workspace alternative webui
oc -p <name>                           # switch to named profile

oc plugins  / skills  / doctor         # listings + multi-layer health
oc config show / variants / init       # config dump / bundled variants / wizard
oc profile list/create/use/path        # profile management

oc memory audit [--user] [--all] [--interactive]                # MEMORY.md / USER.md inspection
oc memory dream-v2-rescore --apply --promote-threshold N        # DREAMS.md re-score
oc context show [--current] [<id>] / list                       # session context %
oc usage sessions                                               # per-session cost + compactions
oc sessions tree                                                # subagent lineage view

oc parity-doctor run                                            # OpenClaw parity audit
oc hooks list / test / doctor                                   # hook diagnostics
oc evolution dashboard                                          # skill-evolution + dreaming state
oc evolution-tuning status / tune / reset                       # auto-tuned thresholds
oc model picker                                                 # provider/model switcher
oc auth login/logout                                            # provider tokens
oc worktrees / checkpoints                                      # checkpoint hygiene + GC
oc pin / unpin                                                  # pin sessions to top of MRU
```

### Test / lint

```bash
pytest tests/                                          # ~15k tests, ~1,200 test files
pytest tests/test_workspace_discovery.py -v            # one file
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Always run the FULL suite before pushing — per-feature passes have hidden snapshot / runtime regressions in the past.

### Cut a release

See `RELEASE.md`. Bump version in two places, tag `vX.Y.Z`, push. CI handles PyPI via OIDC.

---

## 6. Plugin / hook reference

Load-bearing for anyone touching `extensions/` or writing hooks.

**Plugin registration is Python-declarative, not YAML.** No `manifest.yaml` / `manifest.toml`. Each plugin has a `plugin.py` with `register(api)` that constructs a `PluginManifest` from `plugin_sdk`. Some plugins also ship a `plugin.json` for cheap two-phase discovery metadata.

**Manifest schema v4 (optional fields; v3 manifests parse unchanged):**

- `min_host_version` — PEP 440 / semver / calver; enforced BEFORE entry-module import. Mismatch → `PluginIncompatibleError` + skip.
- `activation` — manifest-declared triggers: `on_providers`, `on_channels`, `on_commands`, `on_tools`, `on_models`. Falls back to legacy `tool_names` inference.
- `setup.providers[].auth_choices` — per-method `label`, `cli_flag`, `option_key`, `group`, `onboarding_priority`.
- `plugin.json` is JSON5-tolerant via two-tier parse (`json.loads` → `json5.loads` only on decode error). 256KB cap; pathological files skipped with WARN.
- `plugin_sdk.SecretRef` + `SecretResolver` — typed wire primitive whose `model_dump()` never includes the value.
- `ErrorCode` + `WireResponse.code` — typed wire-error categories.

**Tool-name collisions** caught at registry load (`ToolRegistry` raises `ValueError`). Names are PascalCase (Edit, MultiEdit, Read, TodoWrite, …).

**Settings-based hooks** — declare shell hooks without writing a plugin via `hooks:` in `<profile>/config.yaml`:

```yaml
hooks:
  PreToolUse:
    - matcher: "Edit|Write|MultiEdit"
      command: "python3 /path/to/linter.py"
      timeout_seconds: 10
  Stop:
    - command: "bash /path/to/cleanup.sh"
```

**Hook wire protocol:**
- **stdout JSON** (preferred): `{"action": "block", "message": "..."}` or `{"decision": "block", "reason": "..."}` → block. `{"action": "approve"|"allow"}` → pass. `{"context": "..."}` on PRE_LLM_CALL appends to user message. Malformed JSON → fall back to exit-code path.
- **Exit code** (fallback): `0` → pass, `2` → block with stderr, anything else → fail-open warn+pass.
- **Timeouts / crashes** — fail-open. A wedged hook must never wedge the loop.

Env vars: `OPENCOMPUTER_EVENT`, `OPENCOMPUTER_TOOL_NAME`, `OPENCOMPUTER_SESSION_ID`, `OPENCOMPUTER_PROFILE_HOME`, plus `CLAUDE_PLUGIN_ROOT` aliased to profile home so Claude Code hook scripts drop in unchanged.

**Bundled settings variants:** `lax.yaml`, `strict.yaml`, `sandbox.yaml` under `opencomputer/settings_variants/`. Bootstrap via `oc config init --variant <name>` — the init verifies the copy re-parses before confirming.

---

## 7. Non-obvious gotchas (burned-in lessons)

1. **Plugin module-cache collisions.** When multiple plugins share sibling file names (`plugin.py`, `provider.py`), Python's `sys.modules` returns the first-loaded one for all imports. `plugins/loader.py` uses synthetic unique module names via `importlib.util.spec_from_file_location` + `_clear_plugin_local_cache()` between loads.

2. **Claude Router proxy rejects x-api-key.** Some Anthropic proxies forward `x-api-key` unchanged to upstream Anthropic, which then rejects the proxy_key. `extensions/anthropic-provider/provider.py` supports `ANTHROPIC_AUTH_MODE=bearer` — uses `Authorization: Bearer` AND strips `x-api-key` via an httpx event hook before send.

3. **Compaction MUST preserve `tool_use`/`tool_result` pairs atomically.** Splitting them causes Anthropic's API to 400. `CompactionEngine._safe_split_index` walks back from the naive split until outside any pair.

4. **`DelegateTool._factory` needs `staticmethod` wrap.** Lambdas stored as class attributes get bound to `self` when accessed via instances. `set_factory` uses `cls._factory = staticmethod(factory)`.

5. **asyncio subprocesses can't cross event loops.** A process started in one `asyncio.run()` can't be awaited in another. Background-process tests must do spawn + check + kill in one `asyncio.run()` call.

6. **The plugin SDK boundary is test-enforced.** `tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer` asserts `plugin_sdk/` has no `from opencomputer` imports. `tests/test_plugin_extension_boundary.py` freezes the inventory of existing `extensions/*.py → opencomputer.*` violations (35 files at last count, see `tests/fixtures/plugin_extension_import_boundary_inventory.json`); any NEW violator or stale entry fails CI. Refresh inventory only as last resort via `scripts/refresh_extension_boundary_inventory.py`.

7. **HookContext.runtime is optional for backwards compat.** New hooks should read modes through `effective_permission_mode(ctx.runtime)` rather than `ctx.runtime.plan_mode` / `ctx.runtime.yolo_mode` directly — the helper accounts for slash-command toggles in `runtime.custom`.

8. **Typer auto-promotes single-command apps.** A `typer.Typer(name="X")` with exactly one `@app.command(...)` collapses to a no-subcommand CLI. Always register a second command. See `cli_context.py` (`show` + `list`).

9. **`AgentLoop._runtime` aliases the module-shared `DEFAULT_RUNTIME_CONTEXT` at `__init__` time.** Writes from methods called BEFORE `run_conversation` leak across instances in the same process (test pain). `run_conversation` rebinds per call so production paths are fine; unit tests must rebind: `loop._runtime = RuntimeContext()`.

10. **Counter telemetry must never break the loop.** Anything bumping a per-session counter follows three-tier swallow: `SessionDB.<method>` catches `sqlite3.Error` + returns sentinel; the `AgentLoop` helper catches broad exception + logs WARNING; the slash/CLI renderer falls back to empty-state.

11. **Editable install + worktrees / merges.** After `git worktree add` OR any merge, run `pip install -e . --no-cache-dir --no-deps` + `hash -r`. The `oc` shim goes stale otherwise. `source ~/.zshrc` does NOT refresh the exec cache.

12. **`enable_probe=False` is load-bearing on hot paths.** Compaction / context-window probes hit the network and `~/.opencomputer/<profile>/.context_window_cache.json`. Callers that render in tight loops MUST pass `enable_probe=False` to get static defaults. Active branch is fixing leaks of probe-on through call chains that should be probe-off.

13. **Silent `except: log.debug(...)` hides feature breakage.** Recently audited across `opencomputer/webui/` shims (9 swallow sites → WARN). Pattern reviewer: any generic `except` in a try/except should log at WARN minimum and assert the expected side effect fired, not just absence of exception.

14. **Parallel sessions, one worktree = catastrophe.** Two Claude sessions touching the same checkout race git index + venv state. Use `git worktree add` per session; never share a tree. If you see "[gone]" branches, the `/clean_gone` slash command (from the `commit-commands` plugin) prunes them safely.

---

## 8. User preferences (learned across this project)

- **Action over confirmation.** Just do things. Don't ask "should I…?" — do what can be done, then report.
- **Per-phase workflow is a hard rule:** after each phase, review → `git push` → next phase. Never chain phases without the commit boundary.
- **Concise communication.** Short responses. Direct. Don't pad.
- **Production-grade, never MVP.** Every feature ships end-to-end. No "minimal scaffolding" promises that defer the actual surface.
- **Brutal self-audit on declaring done.** When asked "are you sure?" or "be brutal", walk the spec line-by-line, run the full suite, verify side effects on disk. Frame skips honestly ("deferred" vs "transitively covered" vs "not required").
- **Plugins + skills + MCPs FIRST.** If a relevant tool exists, use it rather than guessing or coding from scratch.

---

## 9. If you need to dig deeper

**Auto-memory ship history:** `~/.claude/projects/-Users-saksham-Vscode-claude/memory/MEMORY.md` — every shipped feature, PR#, date, condensed scope.

**docs/ tree** (curated by topic):

- Architecture & contracts: `sdk-reference.md`, `memory-architecture.md`, `context-window-deep-dive.md`, `databases.md`, `acp.md`, `channels-ownership.md`
- Parity & audits: `OC-FROM-OPENCLAW.md`, `openclaw-parity-2026-05-10.md`, `coding-harness-audit.md`
- Operations: `security-production.md`, `parallel-sessions.md`, `local-models.md`, `mcp-catalog.md`, `memory_dreaming.md`
- Sub-trees: `cli/` (worktrees, files, checkpoints), `runbooks/`, `deployment/` (raspberry-pi, systemd), `providers/`, `integrations/`, `evolution/`, `plugin-authors.md`
- Active specs: `superpowers/specs/` — design docs for in-flight or recent work
- Plans: `docs/plans/` (project-level) + `~/.claude/plans/` (cross-project)

**Reference implementations** cloned at `../sources/`: `claude-code/`, `hermes-agent/`, `openclaw/`, `kimi-cli/`, `hermes-workspace/`. Extraction notes in `docs/refs/<repo-name>/`.

**GitHub:** https://github.com/sakshamzip2-sys/opencomputer
