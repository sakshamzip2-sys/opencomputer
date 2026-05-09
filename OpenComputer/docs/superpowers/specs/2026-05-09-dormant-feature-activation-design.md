# Dormant-Feature Activation — 4-Milestone Plan (Design)

**Date:** 2026-05-09
**Source:** Saksham's 32-item dormant-feature audit
**Status:** auto-approved (auto mode); design drafted from triage

---

## 1. Goal

Convert Saksham's 32-item dormant-feature list into a small set of code+config changes
that turn off the "everything ships off-by-default and stays empty" failure mode without
populating any user-personal data.

Triage:

| Bucket | Count | Examples |
|---|---|---|
| Real code/wiring bugs | 5 | memory-mem0 register collision, /voice import, telegram dual-daemon, missing aliases, launchd dead-not-running |
| Garbage cleanup | 1 | 13 of 19 cron jobs are noise (`a`, `x`, `T`, `b`, `blogwa` ×3) |
| Empty configuration | 15 | MCP / bindings / agents / presets / rules / adapters / hooks |
| Services dormant | 6 | langfuse, wire, dashboard, api-server, auto-mode |
| Personal data | 5 | profile content, user-model graph, accepted skills (out of scope) |

## 2. Scope (4 milestones, 4 PRs)

### M1 — Real bugs + aliases + cron noise prune (PR-1)

| ID | Issue | Fix |
|---|---|---|
| B1 | `memory-mem0` plugin register raises `ValueError: a memory provider is already registered` (collides with honcho); spews stack trace on every `oc doctor` and gateway boot | In `extensions/memory-mem0/plugin.py`, wrap the duplicate-provider case: log a warning + skip registration instead of letting the exception bubble. Honcho wins by default registration order. |
| B2 | `/voice` slash registration fails: `No module named 'slash_commands.voice_cmd'` (absolute import broken when extension is loaded as synthetic module) | In `extensions/voice-mode/plugin.py`, replace `from slash_commands.voice_cmd import VoiceCommand` with file-path import via `importlib.util.spec_from_file_location` (mirrors the existing pattern in `loader.py`) |
| B3 | Two telegram daemons running same bot token (PID 667 launchd + PID 73440 stray homebrew install) | `oc gateway status` already detects this — extend `oc doctor` warning to print exact `kill <PID>` command for each rogue process |
| B4 | Missing CLI aliases: `oc webhooks` / `oc eval list` / `oc checkpoints list` / `oc routing` / `oc adapter list` | Register typer aliases (or thin shim subcommands that forward to the real command) |
| B5 | `oc service start` doesn't actively launch the launchd plist (only enables it, then says "running" without verifying) | After enable, run `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.opencomputer.gateway.plist` (or kickstart) and verify with `launchctl print`. Mac-only path; existing systemd path stays. |
| B6 | 13 of 19 cron jobs are noise — duplicate blogwa, single-letter test names | Add `oc cron prune --noise` flag that proposes deletion of jobs with: name length < 4 chars OR exact-duplicate-by-(name, schedule, prompt) tuples. Interactive confirmation. |

### M2 — `oc activate` wizard (PR-2)

New top-level command: `oc activate [feature]`.

Walks through 5 sub-areas in fixed order. Each detects empty state, proposes a default,
asks user to confirm/edit/skip, writes to the right file. Idempotent — running twice
on a fully-populated profile is a no-op.

Sub-areas (in order):

1. **MCP** — Detect 0 servers in config.yaml. Offer 3 starter stubs:
   `filesystem` (mcp-filesystem-server, scoped to ~/Documents), `github` (uvx mcp-server-github),
   `fetch` (uvx mcp-server-fetch). User confirms which to enable; we write to `config.yaml`.
2. **Agent templates** — Detect only `code-reviewer` (bundled). Offer 3 user-template
   starters from M3 bundle: `test-writer`, `doc-writer`, `planner`. Copy from
   `opencomputer/agents/_starters/` to `~/.opencomputer/<profile>/agents/`.
3. **Bindings** — Detect 0 bindings. Offer "default-route everything to default profile"
   one-liner — that's the most common config and silences the dormant-rule warning.
4. **Presets** — Detect 0 presets. Offer one starter: `minimal` (= no extension plugins).
   Useful as an A/B baseline for `oc plugin perf`.
5. **Rules** — Detect 0 path-glob rules. Offer one starter: deny `**/*.env` writes.
   This is universally useful.

Ordering constraint: MCP → agents → bindings (bindings reference agents) → presets → rules.

The wizard writes to:
- `~/.opencomputer/<profile>/config.yaml` (MCP)
- `~/.opencomputer/<profile>/agents/*.md` (agent templates)
- `~/.opencomputer/<profile>/bindings.yaml` (bindings)
- `~/.opencomputer/<profile>/presets.yaml` (presets)
- `~/.opencomputer/<profile>/rules.yaml` (rules)

Non-interactive flag `--accept-defaults` writes all 5 with default values for CI/scripted use.

### M3 — Sensible defaults bundle (PR-3)

Ship the starter content M2 references:

| Path | Content |
|---|---|
| `opencomputer/agents/_starters/test-writer.md` | Frontmatter + system prompt for "given changed code, write pytest test cases" |
| `opencomputer/agents/_starters/doc-writer.md` | Frontmatter + system prompt for "update README/docstrings to match recent changes" |
| `opencomputer/agents/_starters/planner.md` | Frontmatter + system prompt for "decompose a feature request into milestones" |
| `opencomputer/_starters/mcp_servers.yaml` | Commented stubs for filesystem / github / fetch |
| `opencomputer/_starters/bindings.example.yaml` | Default-route example, channel-pinned example |
| `opencomputer/_starters/presets.example.yaml` | `minimal`, `coding`, `chat-only` |
| `opencomputer/_starters/rules.example.yaml` | Deny .env writes; require approval on `rm -rf` |

The starters live in package data (so `pip install opencomputer` ships them) and are
read by both the wizard (M2) and `oc agents` / `oc bindings` doctors that suggest them
when state is empty.

### M4 — Service helpers (PR-4)

Three commands that turn "this env is dead" into a one-liner.

| Command | Behavior |
|---|---|
| `oc service start` (extend B5) | Already in M1; here we add `oc service status --watch` polling helper |
| `oc langfuse up\|down\|status` | Wraps `docker compose -f <bundled>/langfuse-compose.yaml up -d`. `status` checks port 3000 reachability. Exists partially as `oc langfuse` group; we add `up`/`down`/`status` subcommands. |
| `oc wire start --bg [--port 18789]` | Spawns `opencomputer wire` as detached daemon, writes pidfile to `~/.opencomputer/<profile>/wire.pid` |
| `oc dashboard start --bg [--port 8765]` | Same pattern as wire |

Auto-start hook: `oc setup --enable-services` (new flag) prompts user to set launchd / systemd
auto-start. Default OFF; opt-in.

## 3. Non-Goals (explicit YAGNI)

- **Personal profile content.** Coding/saksham/stock profiles staying empty is by design — M2 surfaces them via the wizard but won't populate MEMORY/USER without user input.
- **Auto-evolved skills.** Need session activity to accumulate; can't be force-seeded.
- **Policy engine progression.** Phase A→B→C requires real safe-decision counts; we don't fake them.
- **Rebuilding the kanban orchestrator.** PRs #567/#568 already shipped the fix; user-facing UX validation is on Saksham, not on this plan.
- **Channel directory population.** Self-heals as messages flow.
- **api-server bind-to-port automation.** `oc service start` covers the launchd surface; api-server is a plugin and should be activated through the existing plugin path.

## 4. Architecture

### M1 file-by-file

- `extensions/memory-mem0/plugin.py` — wrap register call in try/except for ValueError, log warning
- `extensions/voice-mode/plugin.py` — replace absolute import with file-path import
- `opencomputer/cli.py` — add aliases: `webhooks` (alias of `webhook`), `routing` (alias of `bindings`), and forward `eval list`→`history`, `checkpoints list`→`status`, `adapter list`→`adapter ls` (or implement if missing)
- `opencomputer/cli_service.py` — `service start` actively launches launchd; verify with `launchctl print`
- `opencomputer/cli_cron.py` — `cron prune --noise` filter
- `opencomputer/doctor.py` — telegram dual-daemon: print specific `kill PID` commands

### M2 new module

- `opencomputer/cli_activate.py` — typer command group with 5 sub-flows
- `opencomputer/activate/` — small package with one module per sub-area: `mcp.py`, `agents.py`, `bindings.py`, `presets.py`, `rules.py`. Each exports `detect()`, `propose()`, `apply()`.

### M3 package data

- Add `[tool.hatch.build.targets.wheel.shared-data]` entries (or use existing
  `package-data` in pyproject) so `_starters/` ships in the wheel.

### M4 reuse

- `oc langfuse` group already exists. Add `up/down/status`.
- Wire daemon: borrow detached-process pattern from existing `oc gateway start --detached`.
- Dashboard daemon: same.

## 5. Testing strategy

Per [No Push Without Deep Testing] memory rule, every PR runs:
1. `pytest tests/ -x` (full suite)
2. `ruff check opencomputer/ plugin_sdk/ extensions/ tests/`
3. CI green on GitHub Actions before merge

Per-PR test additions:

- M1: 6 unit tests (one per fix) + 1 integration test that runs `oc doctor` and asserts no traceback
- M2: 5 unit tests (one per sub-area), 1 integration test that runs full wizard non-interactively (`--accept-defaults`)
- M3: 1 test asserting all 7 starter files exist in the wheel after `pip install`
- M4: 3 unit tests (start/stop/status for each of langfuse/wire/dashboard) — gated on `docker` / port availability with skip markers

## 6. Risks + mitigations

| Risk | Mitigation |
|---|---|
| memory-mem0 graceful skip masks a real config error where user *wanted* mem0 | Add explicit doctor warning when mem0 enabled but skipped due to collision |
| /voice fix could miss other absolute-import sites | Grep for `from slash_commands\.` in all extensions; fix all at once |
| Service start could hang on slow launchd reload | Timeout = 10s; on timeout, fall back to printing manual command |
| Wizard writes user-visible config files — if format wrong, user has to fix manually | Validate after write: re-parse the written YAML; rollback to .bak on parse failure (same pattern as `oc config init`) |
| Cron prune deletes a job the user wanted | Default to dry-run; `--apply` required to delete |

## 7. Rollout

Ship in dependency order: M1 → M2 → M3 (parallel with M2) → M4.

Each PR uses git worktrees per `[Worktrees for Parallel Sessions]` memory rule.
Each PR squash-merges with conventional-commit subject. CI must be green.

## 8. Out of scope (deferred to user)

After all 4 PRs merge, Saksham's manual followups are:

1. Run `oc activate --accept-defaults` to seed his default profile.
2. Edit the seeded `bindings.yaml` to point coding/saksham/stock channels.
3. Populate MEMORY/USER for non-default profiles.
4. Optionally enable launchd/docker services via `oc setup --enable-services`.

These are personal-data tasks; the framework can scaffold but cannot populate them.
