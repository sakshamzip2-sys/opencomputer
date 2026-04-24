# Changelog

All notable changes to OpenComputer are listed here. Follows [Keep a Changelog](https://keepachangelog.com/) conventions, [semver](https://semver.org/).

## [Unreleased]

### Added (Phase B4 — Prompt evolution + monitoring dashboard + atrophy detection, parallel Session B)

- **Migration `002_evolution_b4_tables.sql`** — adds three new tables to the evolution DB: `reflections` (track each `reflect()` invocation: timestamp, window_size, records_count, insights_count, records_hash, cache_hit), `skill_invocations` (atrophy data: slug + invoked_at + source ∈ {`manual` | `agent_loop` | `cli_promote`}), `prompt_proposals` (id + proposed_at + target ∈ {`system` | `tool_spec`} + diff_hint + insight_json + status ∈ {`pending` | `applied` | `rejected`} + decided_at + decided_reason). All with appropriate indexes. Migration is idempotent + automatic via the existing `apply_pending()` runner.
- **`PromptEvolver`** (`opencomputer/evolution/prompt_evolution.py`) — takes `Insight` with `action_type=="edit_prompt"` and persists it as a **diff-only proposal**. **Never auto-mutates a prompt file.** Writes a row to `prompt_proposals` table + atomic sidecar `<evolution_home>/prompt_proposals/<id>.diff` (via `tmp + .replace`). Validates `target` ∈ {`system`, `tool_spec`} and that `diff_hint` is non-empty. CLI: `prompts list/apply/reject` — `apply` records the user decision but does NOT edit prompt files (caller's responsibility — by design). `PromptProposal` is a frozen+slots dataclass mirroring DB rows.
- **`MonitorDashboard`** (`opencomputer/evolution/monitor.py`) — aggregates: total reflections + last-reflection timestamp, list of synthesized skills with invocation counts + atrophy flags, average reward score over last 30 days vs lifetime. Atrophy threshold default: 60 days no-invocation. `_iter_reward_rows()` queries `trajectory_records.reward_score` directly (option-b: keeps `TrajectoryRecord` dataclass shape stable; no breaking change for downstream consumers). CLI: `dashboard` renders two Rich tables (summary + per-skill).
- **Storage helpers** added to `opencomputer/evolution/storage.py`: `record_reflection`, `list_reflections`, `record_skill_invocation`, `list_skill_invocations`, `record_prompt_proposal`, `list_prompt_proposals`, `update_prompt_proposal_status`. All follow the existing `conn=None` lazy-open pattern.
- **CLI extensions** in `opencomputer/evolution/cli.py`: new `prompts` subapp (`list/apply/reject`), top-level `dashboard`, `skills retire` (moves to `<evolution_home>/retired/<slug>/` for audit trail; collision-safe with `-2..-N` suffixes), `skills record-invocation` (manual analog of B5+ auto-recording from agent loop). The existing `reflect` command now records a `reflections` row after each call; `skills promote` records an initial `cli_promote` invocation so promoted skills don't appear atrophied immediately.
- **Tests** — 58 new across 4 files (`tests/test_evolution_{storage_b4,prompt_evolution,monitor,cli_b4}.py`). Full suite: **1326 passing** (was 1268 entering B4). Zero edits to existing tests; zero changes to Session-A-reserved files.

**B4 design philosophy:** prompt evolution NEVER auto-applies. Atrophy detection is informational only — `skills retire` is a user-invoked move, not automatic. Together with B1+B2's quarantine-namespace design, evolution remains entirely opt-in and reversible at every step.

### Added (Phase B2 — Evolution reflection + skill synthesis + CLI, parallel Session B)

- **GEPA-style reflection engine** (`opencomputer/evolution/reflect.py`) — `ReflectionEngine.reflect(records)` renders the Jinja2 prompt (`prompts/reflect.j2`), calls the configured `BaseProvider` (via OpenComputer's plugin registry — never direct Anthropic SDK), parses JSON output, and returns a list of `Insight` objects. Defensive JSON parser strips markdown fences, skips malformed entries, filters `evidence_refs` against actual record ids (catches LLM hallucinations). Per-call cache keyed by sha256 of the record-id sequence, so dry-runs and retries don't re-bill the LLM.
- **Skill synthesizer** (`opencomputer/evolution/synthesize.py`) — `SkillSynthesizer.synthesize(insight)` writes a III.4-hierarchical skill (`SKILL.md` + optional `references/` + `examples/`) into the evolution quarantine namespace at `<profile_home>/evolution/skills/<slug>/`. **Atomic write** via `tempfile.mkdtemp` + `os.replace` — half-written skills are impossible. **Path-traversal guard** rejects reference/example names containing `/`, `\`, or leading `.` (defense against LLM payloads that try to write outside the skill dir). **Slug collision** handling: appends `-2`, `-3`, …, `-99` suffixes; never overwrites.
- **`opencomputer evolution …` CLI subapp** (`opencomputer/evolution/{entrypoint,cli}.py`) — Typer subapp wired through `entrypoint.py::evolution_app` so Session A folds it into `cli.py` in a single line (`app.add_typer(evolution_app, name="evolution")`). Until then, invoke directly via `python -m opencomputer.evolution.entrypoint <subcommand>`. Commands:
  - `reflect [--window 30] [--dry-run] [--model claude-opus-4-7]` — manual reflection trigger; `--dry-run` shows the trajectory table without an LLM call.
  - `skills list` — Rich table of synthesized skills + their description.
  - `skills promote <slug> [--force]` — copy from quarantine to user's main skills dir; refuses overwrite without `--force`.
  - `reset [--yes]` — delete the entire evolution dir (DB + quarantine + future prompt-proposals); confirms before wiping unless `--yes`. **Session DB and main skills are untouched.**
- **Jinja2 prompt templates** (`opencomputer/evolution/prompts/{reflect,synthesize}.j2`) — `reflect.j2` renders trajectory batches into a single LLM prompt asking for high-confidence Insight extraction (system framing emphasizes conservatism; output schema is JSON-only with payload contracts documented inline). `synthesize.j2` renders SKILL.md with YAML frontmatter, the `<!-- generated-by: opencomputer-evolution -->` quarantine marker, and traceability comments (slug, confidence, evidence-refs).
- **Tests** — 36 new (`tests/test_evolution_{reflect_template,reflect_engine,synthesize_skill,cli}.py`); 1 obsolete stub-behavior test removed; full suite at 1070 passing across 60 test files (was 1058 entering B2). **Zero edits to existing test files**; no Session-A-reserved file touched.

### Added (Phase B1 — Evolution subpackage skeleton, parallel Session B)

- **`opencomputer/evolution/` subpackage** — self-contained scaffold for GEPA-style self-improvement (trajectory collection → reflection → skill synthesis). **Opt-in** by design (`config.evolution.enabled` defaults to `False`); nothing runs unless invoked. See `docs/evolution/README.md` (user-facing) and `docs/evolution/design.md` (architecture).
- **Trajectory dataclasses** (`evolution/trajectory.py`) — `TrajectoryEvent` and `TrajectoryRecord` (frozen+slots). Privacy-first: `metadata` string values >200 chars are rejected at construction time, so raw prompt text can never leak into the evolution store. Helpers `new_event` / `new_record` / `with_event` for ergonomic immutable-append flow.
- **SQLite storage with self-contained migration runner** (`evolution/storage.py` + `evolution/migrations/001_evolution_initial.sql`) — separate DB at `<profile_home>/evolution/trajectory.sqlite` (no contention with `sessions.db`). WAL mode + retry-with-jitter, matching `agent/state.py` pattern. Migration runner tracked via `schema_version` table; documented as a temporary self-contained shim that will refactor onto Sub-project F1's framework once that lands (`# TODO(F1)` marker at top of file).
- **Rule-based reward function** (`evolution/reward.py`) — `RewardFunction` runtime-checkable Protocol + `RuleBasedRewardFunction` default. Three weighted signals (tool success rate 0.5, user-confirmed cue 0.3, completion flag 0.2). Conservative — no length component (verbose responses NOT rewarded), no latency component. LLM-judge reward explicitly post-v1.1.
- **Reflection + synthesis stubs** (`evolution/reflect.py`, `evolution/synthesize.py`) — `Insight` frozen dataclass (observation + evidence_refs + action_type + payload + confidence) + `ReflectionEngine` and `SkillSynthesizer` classes whose constructors accept the parameters B2 will need (provider, window, dest_dir) but whose work-doing methods raise `NotImplementedError("...lands in B2...")`. Public API surface locked at B1 so consumers can be wired against a stable contract today.
- **Hermes deep-scan + design doc** — `docs/evolution/source-map.md` (474-line architecture summary of the Nous Research Hermes Self-Evolution reference, MIT-licensed) + `docs/evolution/design.md` (architectural decisions, divergences from Hermes, self-audit, refactor paths).
- **Parallel-session coordination protocol** — `docs/parallel-sessions.md`: shared state file documenting reserved files (Session A vs Session B), bus-API change log, PR-review responsibilities, rollback procedure. Both sessions read at startup, update after each commit.

73 new tests (`tests/test_evolution_{trajectory,storage,reward,reflect,synthesize}.py`); zero changes to existing files (Session-A-reserved territory respected).

### Changed (pre-v1.0 stabilization — drift-preventer cleanup)

- **Consolidated plugin search-path construction.** New single source of truth: `opencomputer.plugins.discovery.standard_search_paths()`. Four call sites that previously duplicated the `profile-local → global → bundled` walk now import it: `cli._discover_plugins`, `cli.plugins` (listing command), `cli_plugin.plugin_enable`, `AgentLoop._default_search_paths`. No behavior change except for one fix — see next bullet.
- **Fix: `opencomputer plugins` listing command now honors profile-local plugins.** Previously it built its own search path that skipped the profile-local dir and ordered bundled before user-installed (wrong priority for dedup). It now matches every other plugin-walking code path. Run `opencomputer -p <name> plugins` to see a named profile's locally-installed set.

### Changed — BREAKING (pre-v1.0 tool-name renames)

Three tool-name changes landed in the pre-v1.0 window. Any existing user transcript or external integration that invoked these tools by their old names will fail at load. Post-v1.0 these would require a semver-major bump; doing them now is the right window.

- **`Diff` → `GitDiff` and `CheckpointDiff`** — two different plugins previously registered a tool named `Diff` with different semantics (`extensions/dev-tools` = git diff wrapper; `extensions/coding-harness` = unified diff vs rewind checkpoint). The collision triggered `ToolRegistry` `ValueError` when both plugins loaded in the same profile, and when they didn't, it was a latent LLM-selection bug (the model would pick the anonymous "default" Diff unpredictably). Both are now semantically precise: dev-tools ships `GitDiff`, coding-harness ships `CheckpointDiff`.
- **`start_process`, `check_output`, `kill_process` → `StartProcess`, `CheckOutput`, `KillProcess`** — the last snake_case tool names in the codebase, now aligned with the PascalCase convention every other tool uses (Edit, MultiEdit, Read, TodoWrite, Rewind, GitDiff, CheckpointDiff, RunTests, ExitPlanMode, ...). Class names (`StartProcessTool`, etc.) were already PascalCase — only the `ToolSchema.name` the LLM sees was inconsistent.

All 809 tests green across the four atomic commits.

### Added (Phase 12b1 — Honcho as default memory overlay)

- **Honcho is the default memory provider when Docker is available.** Setup wizard auto-starts the 3-container stack (api + postgres+pgvector + redis + deriver) via `bootstrap.ensure_started()` — no prompt, no opt-in. On machines without Docker, the wizard prints the install URL and persists `provider=""` so the next run doesn't retry. Baseline memory (MEMORY.md + USER.md + SQLite FTS5) stays on unconditionally.
- **`RuntimeContext.agent_context`** — typed `Literal["chat","cron","flush","review"]` = `"chat"`. `"cron"`/`"flush"` short-circuit both `MemoryBridge.prefetch` AND `sync_turn` so batch jobs don't spin the external stack. Mirrors Hermes' `sources/hermes-agent/plugins/memory/honcho/__init__.py:279-286`.
- **`HonchoSelfHostedProvider.mode`** — `Literal["context","tools","hybrid"]` = `"context"`. Validates at construction. `context` injects recall automatically; `tools` exposes Honcho as agent-facing tools; `hybrid` does both. Consumed by A5 wizard / A7 loop-wiring.
- **`bootstrap.ensure_started(timeout_s=60)`** — idempotent bring-up helper. Pre-flight Docker detection, port-collision check (only port 8000 is host-exposed), `docker compose pull --quiet`, `docker compose up -d`, health-poll every 2s until timeout. Returns `(ok, msg)`. Replaces direct `honcho_up()` in the wizard.
- **`PluginManifest.enabled_by_default: bool = False`** — new manifest field. `memory-honcho/plugin.json` sets it to `true`; other plugins preserve existing behavior. Schema + dataclass + `_parse_manifest` updated atomically per `opencomputer/plugins/CLAUDE.md`.
- **`opencomputer memory doctor`** — 5-row Rich table reporting the state of every memory layer (baseline / episodic / docker / honcho / provider). Diagnostic, always exits 0. Complements `memory setup` / `status` / `reset`.
- **AgentLoop wires MemoryBridge at last** — `run_conversation` now calls `memory_bridge.prefetch(user_message, turn_start_index, runtime)` after appending the user message + before the tool loop, and `memory_bridge.sync_turn(user, assistant, turn_index, runtime)` on END_TURN (same site as the Phase 12a reviewer spawn). Prefetch output is appended to the per-turn `system` variable as `"## Relevant memory"`; the frozen `_prompt_snapshots[sid]` is NOT modified — preserves the prefix-cache invariant. The cron/flush guard from A1 now operates end-to-end in production.

### Added (Phase 14 — multi-profile support)

- **Per-profile directories + `-p` flag routing** (14.A). `_apply_profile_override()` in `opencomputer/cli.py` intercepts `-p` / `--profile=<name>` / `--profile <name>` from `sys.argv` BEFORE any `opencomputer.*` import, sets `OPENCOMPUTER_HOME`, and all downstream `_home()` consumers resolve to the active profile's directory automatically. 14.M/14.N code becomes profile-aware with zero changes.
- **Sticky active profile** at `~/.opencomputer/active_profile` (one-line file). `opencomputer profile use <name>` writes it; `opencomputer profile use default` unlinks.
- **Pre-import explicit-flag wins over parent env** — a `-p coder` always overrides `OPENCOMPUTER_HOME` even if a parent shell exported it. Guard on sticky-file read only, not on the explicit-flag write.
- **`opencomputer profile` CLI** (14.B) — `list`, `create`, `use`, `delete`, `rename`, `path`. Create supports `--clone-from <other>` (config-only) and `--clone-all` (full recursive state copy). Rename warns about Honcho continuity loss. Delete clears sticky if the deleted profile was active.
- **Plugin manifest scoping** (14.C) — `PluginManifest` gains `profiles: tuple[str, ...] | None = None` (omit or `["*"]` = any profile; concrete list = restricted) and `single_instance: bool = False`. Manifest validator accepts both plus `schema_version`. `opencomputer/plugins/discovery.py` populates the new fields from `plugin.json`.
- **Manifest-layer enforcement in loader** (14.D) — Layer A: `_manifest_allows_profile()` in `opencomputer/plugins/registry.py` gates loading by the plugin's declared compatibility. Composes with the existing Layer B enabled-ids filter (both must pass). Skips log at INFO with profile + reason for diagnostics.
- **Profile-local plugin directory** (14.E) — `~/.opencomputer/profiles/<name>/plugins/`. Discovery scans in priority order: profile-local → global (`~/.opencomputer/plugins/`) → bundled (`extensions/`). Profile-local shadows global shadows bundled on id collision.
- **`opencomputer plugin` subcommand** (14.E) — `install`, `uninstall`, `where`. `install <path>` defaults to the active profile's local dir; `--global` targets the shared dir; `--profile <name>` targets a specific profile. `--force` to overwrite. `where <id>` prints the first match across the priority-ordered roots.
- **Reserved profile names** — `default`, `presets`, `wrappers`, `plugins`, `profiles`, `skills` rejected by `validate_profile_name` (prevent subdir collisions with the root layout).
- **README Profiles + Presets + Workspace overlays + Plugin install sections** (14.L) — user-facing docs for everything above.

### Tests

- `tests/test_phase14a.py` (23 tests): validation + directory resolution + flag routing (short/long/equals forms) + sticky fallback + flag-beats-sticky + argv stripping + invalid-name fallback + parent-env override.
- `tests/test_phase14b.py` (19 tests): all seven profile CLI subcommands including clone-from/clone-all, default-name refusal, confirmation prompts, sticky-file side effects, Honcho rename warning.
- `tests/test_phase14c.py` (10 tests): dataclass defaults, manifest validator accepts profiles/single_instance/schema_version, discovery propagates fields, bundled plugins declare profiles.
- `tests/test_phase14d.py` (8 tests): manifest helper unit tests (None/wildcard/specific/empty list) + loader integration (wildcard loads anywhere, restricted skips mismatched profile, specific-match loads, Layer A + B compose correctly).
- `tests/test_phase14e.py` (11 tests): install defaults to profile-local, --global flag, --profile flag, --force overwrite, refuses existing without --force, rejects source-without-manifest; uninstall, where lookup; discovery priority (profile-local shadows global).

All 488 tests green on this branch.

### Added (Phase 10f — memory baseline completion)
- **`Memory` tool** (`opencomputer/tools/memory_tool.py`) — agent-facing
  curation of MEMORY.md + USER.md. Actions: `add`/`replace`/`remove`/`read`.
  Targets: `memory` (agent observations) / `user` (user preferences).
- **`SessionSearch` tool** (`opencomputer/tools/session_search_tool.py`) —
  agent-facing FTS5 search across all past messages. Default limit 10,
  max 50. Wraps new `SessionDB.search_messages()` returning full content.
- **USER.md support** in `MemoryManager` — separate from MEMORY.md so
  agent observations don't commingle with user preferences.
- **Atomic write pipeline** — `_write_atomic()` + `_file_lock()` (fcntl /
  msvcrt). Every mutation: acquire lock → backup to `<path>.bak` →
  write temp → `os.replace()`. Never leaves partial files.
- **Character limits** on both files, configurable via `MemoryConfig`.
  Over-limit writes raise `MemoryTooLargeError` (returned as tool error).
- **Declarative memory injected into base system prompt** (frozen per
  session) — preserves Anthropic prefix cache across turns.
  `PromptBuilder.build()` gained `declarative_memory`, `user_profile`,
  `memory_char_limit`, `user_char_limit` params.
- **`MemoryProvider` ABC** (`plugin_sdk/memory.py`) — public contract for
  external memory plugins (Honcho, Mem0, Cognee). 5 required methods,
  2 optional lifecycle hooks, cadence-aware via `turn_index`.
- **`InjectionContext.turn_index`** field (default 0, backward compatible).
- **`PluginAPI.register_memory_provider()`** with one-at-a-time guard +
  isinstance check.
- **`MemoryContext` + `MemoryBridge`** — shared deps bag + exception-safe
  orchestrator wired into `AgentLoop`. A broken provider never crashes
  the loop.
- **`opencomputer memory` CLI subcommand group** —
  `show / edit / search / stats / prune / restore` with `--user` flag.

### Changed
- `MemoryConfig` gained: `user_path`, `memory_char_limit=4000`,
  `user_char_limit=2000`, `provider=""`, `enabled=True`,
  `fallback_to_builtin=True`. Backward compatible.

### Tests
- +62 tests in `tests/test_phase10f.py`, all green.
- Full suite: 336 passing.

## [0.1.0] — 2026-04-21 (pre-alpha)

### Added
- Initial public release.
- Core agent loop with tool dispatch (`opencomputer/agent/loop.py`).
- Three-pillar memory: declarative (MEMORY.md), procedural (skills/), episodic (SQLite + FTS5 full-text search).
- 7 built-in tools: Read, Write, Bash, Grep, Glob, skill_manage, delegate.
- Strict plugin SDK boundary (`plugin_sdk/`) with manifest-first two-phase discovery.
- Bundled plugins:
  - `anthropic-provider` — Anthropic Claude models with Bearer-auth proxy support.
  - `openai-provider` — OpenAI Chat Completions + any OpenAI-compatible endpoint.
  - `telegram` — Telegram Bot API channel with typing indicators.
  - `discord` — Discord channel via discord.py.
  - `coding-harness` — Edit, MultiEdit, TodoWrite, background-process tools + plan mode.
- MCP integration — connects to Model Context Protocol servers (stdio), tools namespaced.
- Gateway for multi-channel daemons.
- Wire server — JSON over WebSocket RPC for TUI / IDE / web clients (`opencomputer wire`).
- Streaming responses (Anthropic + OpenAI) with per-turn typing indicators on Telegram.
- Dynamic injection engine — cross-cutting modes as providers (plan mode).
- Hardened context compaction — real token counts, tool-pair preservation, aux-fail fallback.
- Runtime context threading — plan_mode / yolo_mode / custom flags flow loop → hooks → delegate → subagents.
- CLI: `chat`, `gateway`, `wire`, `search`, `sessions`, `skills`, `plugins`, `setup`, `doctor`, `config`.
- Interactive setup wizard (`opencomputer setup`).
- Health check (`opencomputer doctor`).
- Typed YAML config with dotted-key get/set.
- GitHub Actions CI — pytest on Python 3.12 + 3.13, ruff lint.
- 114 tests.

### Credits
Architectural ideas synthesized from [Claude Code](https://github.com/anthropics/claude-code),
[Hermes Agent](https://github.com/NousResearch/hermes-agent),
[OpenClaw](https://github.com/openclaw/openclaw),
[Kimi CLI](https://github.com/MoonshotAI/kimi-cli).

[Unreleased]: https://github.com/sakshamzip2-sys/opencomputer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sakshamzip2-sys/opencomputer/releases/tag/v0.1.0
