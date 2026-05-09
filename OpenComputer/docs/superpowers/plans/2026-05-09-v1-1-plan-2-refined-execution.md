# v1.1 Plan 2 — refined execution (2026-05-09)

Status: in execution.
Origin: refines `2026-05-08-v1-1-plan-2-architecture-features.md` after the same brainstorm-phase audit pattern that produced `2026-05-09-v1-1-plan-1-refined-execution.md`.

## Brainstorm-audit findings

The original plan-2 listed 14 sub-items across M4/M5/M7/M8 with a 4-6 week estimate. Verification against `origin/main` (HEAD `9392b273`) found:

| Item | Original status | Verified state | Action |
|---|---|---|---|
| M4.1 `delegate(isolation="worktree")` | open | TRUE pending — `tools/delegate.py` has no `isolation` schema field | Defer (heavy) |
| M4.2 `delegate(isolation="copy")` | open | TRUE pending | Defer (depends on M4.1's dispatcher shape) |
| M4.3 skill `context: fork` | open | TRUE pending — `tools/skill.py:85` uses `frontmatter.load()` but reads only `body`; `agent` / `tools` / `context` ignored | Defer (works degraded without M4.1) |
| M4.4 skill `tools:` allowlist | open | TRUE pending | Defer (security-sensitive; needs M4.1) |
| M4.5 `oc worktrees prune` | open | DONE — `cli_worktrees.py` has `list` + `clean` ("Remove stale `.opencomputer-worktrees/*` entries"). The plan's `prune` is the existing `clean`. | Skip |
| M5.1 `oc session checkpoints <id>` | open | TRUE pending — `cli_session.py` has `list/show/fork/resume/delete/prune/stats/export/rename` but no `checkpoints`; data is already on disk under `~/.opencomputer/harness/<sid>/rewind/<cid>/` (per `checkpoint_admin.iter_stores`). The work is one new subcommand reading existing data. | **Execute** |
| M5.2 per-prompt checkpoint creation | open | TRUE pending — `extensions/coding-harness/rewind/store.py` has `RewindStore.save()` but no auto-fire on tool_use in `agent/loop.py` | Defer (loop wiring is invasive) |
| M5.3 `oc session rewind` interactive | open | TRUE pending | Defer (depends on M5.2 + picker UX) |
| M5.4 plan-mode post-approval routing | open | TRUE pending | Defer (medium scope) |
| M7.1 path-glob rules loader | open | TRUE pending — no `rules_loader.py`, no `.opencomputer/rules/` discovery | **Execute** |
| M7.2 `oc rules` CLI | open | TRUE pending | **Execute** (combined with M7.1 — same surface) |
| M8.1 `prompt` hook type | open | TRUE pending | Defer |
| M8.2 `agent` hook type | open | TRUE pending (depends on M4.1) | Defer |
| M8.3 PostCompact hook event | open | DONE — already shipped as `HookEvent.AFTER_COMPACTION` (Round 2A P-1, `plugin_sdk/hooks.py:273`); fired at `agent/loop.py:1758` | Skip |

## 9-lens audit on the refined items (M5.1 + M7)

1. **Assumption-check** — `oc session checkpoints <id>` reads the existing on-disk layout; `rules_loader` reads files only. Both have grep-verified premises.
2. **Architecture stress** — M7 rule injection only fires on path-touching tool calls (Read/Write/Edit/MultiEdit/Glob/Grep) so non-path-touching tools (Bash, WebFetch) don't get spurious rules. Edge case: `Edit` with relative path → resolve via cwd before matching.
3. **Alternative dismissal** — Could store rules in DB instead of filesystem files. Filesystem is the right choice because rules are version-controllable per-repo (`.opencomputer/rules/*.md` in git).
4. **Requirement gap** — Need `oc rules check <path>` debugging command for "why isn't my rule firing?" (already in plan; will ship).
5. **Composability** — M5.1 leverages existing `RewindStore`; M7 has clean separation (loader → matcher → injector). They don't share state.
6. **Scope honesty** — M5.1 is ~50 LOC + tests (1 hr). M7 is ~300 LOC across 3 files (~3 hrs). Total ~4 hrs, fits one session.
7. **API surface drift** — M7's `Rule` dataclass + `load_rules()` / `active_rules_for()` API is small. New subcommand is one more `@session_app.command` registration.
8. **Failure modes** — Malformed rule frontmatter → log + skip (don't break agent). Rule body that injects 50KB → cap at 4KB per rule, log truncation. Non-existent `.opencomputer/rules/` → no rules active.
9. **YAGNI** — Rule `priority` field is in plan; ship it because two rules can match same file. Rule `globs_exclude` is rarely needed; defer until first user asks.

## Execution scope (this session — 2 PRs)

### PR-E — M5.1 `oc session checkpoints <id>` subcommand

- Branch: `feat/v1-1-session-checkpoints-2026-05-09` off `origin/main`.
- File: `opencomputer/cli_session.py` — new `@session_app.command("checkpoints")`.
- Reads from `harness_root() / session_id / "rewind"` via `RewindStore.list()`.
- Output: Rich table of `checkpoint_id` (8-char prefix) + `label` + `created_at` + size.
- Flags: `--json` for machine-readable output, `--limit N` (default 50).
- Tests: `tests/test_session_checkpoints_cli.py` covering happy path, missing session, no checkpoints, json mode.
- Effort: ~45 min.

### PR-F — M7 path-glob rules + `oc rules` CLI

- Branch: `feat/v1-1-path-glob-rules-2026-05-09` off `origin/main`.
- New module `opencomputer/agent/rules_loader.py`:
  - `Rule(name, paths, priority, body)` dataclass.
  - `load_rules(rules_dir)` — parses each `*.md`, extracts YAML frontmatter, returns sorted `list[Rule]` (highest priority first).
  - `active_rules_for(rules, paths)` — filters via `fnmatch`; returns matching rules.
  - `format_rules_block(rules)` — renders as `[Active Rules]` markdown for system-prompt injection.
- New module `opencomputer/cli_rules.py` — Typer subapp with `list`, `check <path>`, `show <name>`. Wired into `cli.py` as `@app.add_typer(rules_app, name="rules")`.
- `agent/loop.py` integration: collect path args from path-touching tool calls, look up matching rules, append `[Active Rules]` block to next system prompt (`BEFORE_PROMPT_BUILD` hook is the cleanest hook point).
- Discovery order: workspace `.opencomputer/rules/*.md` overrides profile `~/.opencomputer/<profile>/rules/*.md` of the same name.
- Body cap: 4KB per rule; truncated with marker.
- Tests: `tests/test_path_glob_rules.py` covering loader, matching, priority, workspace-overrides-profile, body cap. `tests/test_cli_rules.py` covering 3 subcommands.
- Effort: ~3 hrs.

### Acceptance gates

- `pytest tests/test_session_checkpoints_cli.py tests/test_path_glob_rules.py tests/test_cli_rules.py` — all new tests pass.
- `pytest -k "session or rules or checkpoints"` — adjacent tests pass.
- `ruff check` — clean.
- Manual smoke: create `.opencomputer/rules/python.md` with `paths: ["**/*.py"]`, run `oc rules check src/foo.py` → shows the rule.

## Deferred items (write-up below for next-session work)

### Tier B — medium scope, doable in next session (1-2 PRs each)

- **M4.3 skill `context: fork`** — extends `tools/skill.py` to consume frontmatter `context` / `agent` / `tools` / `model` fields. Without M4.1 isolation, `context: fork` works but doesn't get its own filesystem sandbox; document the limitation. Effort: 1 day.
- **M5.4 plan-mode post-approval routing** — extends `extensions/coding-harness/tools/exit_plan_mode.py` to include `next_mode`; threads through `RuntimeContext.permission_mode`. Effort: 2 days.
- **M5.2 per-prompt checkpoint creation** — wires `RewindStore.save()` into `agent/loop.py` before each tool_use block. Needs careful tests for snapshot file cost. Effort: 2-3 days.

### Tier C — heavy, multi-session

- **M4.1 `delegate(isolation="worktree")`** — new `agent/worktree.py` module + atexit cleanup + crash-handling tests. The plan's hardest item. Effort: 2 days.
- **M4.2 `delegate(isolation="copy")`** — depends on M4.1's dispatcher; needs `.opencomputer/sandbox.ignore`. Effort: 1 day.
- **M4.4 skill tools/model enforcement** — security-sensitive; needs `skill.execute` capability + audit. Effort: 1-2 days.
- **M5.3 `oc session rewind` interactive** — needs M5.2 + prompt-toolkit picker + headless flags. Effort: 2 days.
- **M8.1 `prompt` hook type** — aux-LLM-backed hook with timeout + token budget. Effort: 2 days.
- **M8.2 `agent` hook type** — depends on M4.1 (`isolation="copy"`). Effort: 2-3 days.

### Skipped (already shipped)

- **M4.5 `oc worktrees prune`** — already shipped as `oc worktrees clean`.
- **M8.3 PostCompact** — already shipped as `HookEvent.AFTER_COMPACTION` in Round 2A P-1.

## Total estimate (refined)

- This session: 2 PRs (~4 hrs).
- Tier B follow-ups: 3 PRs over 1-2 sessions (~5 days work).
- Tier C deferrals: 6 PRs over 2-3 weeks (~13 days work).

The ~4-week estimate stands for the FULL plan-2 surface; this refined doc just sequences the work honestly.

## What this refined plan refuses

- Shipping all of plan-2 in one session (not realistic at 4-6 weeks of work).
- Re-doing M4.5 (`oc worktrees prune`) when `oc worktrees clean` already covers it.
- Re-doing M8.3 (`POST_COMPACT`) when `AFTER_COMPACTION` already exists with the same shape.
- Pre-bundling M4.3 with M4.1 — they CAN ship in parallel since M4.3's `context: fork` works degraded without isolation.
