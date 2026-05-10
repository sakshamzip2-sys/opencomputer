---
name: batch
description: Use when a user asks to perform the same kind of change across N independent units (rename 200 functions, migrate 50 files, audit 100 plugins) — orchestrates parallel subagents in worktree-isolated sandboxes, each opening its own PR
---

# /batch — Parallel N-agent migrations

## When to use

- Mechanical change across many independent files / functions / modules where each unit can be migrated without coordinating with the others.  Examples:
  - Rename a deprecated API call across 200 call sites.
  - Run a codemod against 50 files and verify each individually.
  - Audit 100 plugin manifests for a schema-v5 migration and open one PR per fixed plugin.
  - Generate dependency-update PRs for 30 plugins in a marketplace.
- A naive "loop and edit" approach would either:
  - Lose context (each edit drains the session), or
  - Race against itself when multiple edits touch shared files.

## When NOT to use

- A single change that touches multiple files together (one PR, not N).  Plain edit-and-commit is cheaper.
- Changes whose units depend on each other.  Worktree isolation prevents subagents from coordinating.
- Anything below the worktree-overhead threshold (~3 units).  The shell time to create + clean up worktrees is non-trivial.

## How it works

`/batch` decomposes the inbound task into N atomic units, then spawns up to `max_parallel` subagents (default 30, configurable via the `--max` flag).  Each subagent runs in an isolated git worktree (via `delegate(isolation="worktree")` from M4.1, already on main) so there's no cross-contamination on shared paths.

```
       /batch "rename foo() to bar() in all 50 files"
                    │
                    ▼
       ┌─────────────────────────────┐
       │ 1. Decompose task into     │
       │    N atomic units (LLM)     │
       └─────────────────────────────┘
                    │
                    ▼
       ┌─────────────────────────────┐
       │ 2. For each unit (parallel):│
       │    - git worktree add       │
       │    - delegate(isolation=…)  │
       │    - subagent runs the unit │
       │    - subagent commits + PR  │
       └─────────────────────────────┘
                    │
                    ▼
       ┌─────────────────────────────┐
       │ 3. Aggregate results:       │
       │    - PR URLs                │
       │    - per-unit success/fail  │
       │    - cleanup orphaned trees │
       └─────────────────────────────┘
```

## Steps

1. **Confirm the unit boundary with the user before spawning.**  A bad decomposition wastes 30 worktrees.  Print the proposed unit list, ask "OK to proceed?"
2. **Decompose** the task into atomic units.  Each unit must be independently verifiable (its own tests should pass without depending on other units).
3. **Cap N.**  Default `max_parallel = 30`.  If user requests more, push back: more parallelism rarely helps and it stresses CI.
4. **Spawn each unit** via the `Delegate` tool with `isolation="worktree"`:
   ```
   Delegate(
       task="<unit description with full context>",
       isolation="worktree",
       allowed_tools=["Read", "Edit", "MultiEdit", "Bash", "Grep"],
       max_turns=20,
   )
   ```
5. **Each subagent** is responsible for:
   - Implementing its unit
   - Running tests (`pytest tests/<scope> -x`)
   - Committing its work
   - Opening a PR via `gh pr create` (with the standard footer)
6. **Aggregate** the per-unit outcomes into a single report:
   - Total: N units
   - Successful: list of PR URLs
   - Failed: list of (unit, reason)
   - Aborted: list of units the user stopped before completion
7. **Cleanup.**  After the spawn, run `oc worktrees prune` to remove any orphaned worktrees from crashed subagents (M4.5 already on main).

## Production-grade safeguards

- **Hard cap** on N (default 30; per-invocation max via `--max=N`).  Refuse more.
- **Worktree pre-flight.**  `git worktree list --porcelain` before spawn — if there are >50 active worktrees from prior runs, ask the user to prune first.
- **Per-subagent timeout**: 20 minutes default.  A wedged subagent doesn't block the rest.
- **Graceful failure**: a subagent that crashes does NOT abort sibling subagents.  Its unit is marked "failed" in the aggregate report; the user can re-spawn just that unit.
- **PR title prefix.**  Each subagent's PR is titled `<task-prefix>: <unit-description>` so reviewers can group them in the GitHub UI.
- **No nested batching.**  A `/batch` invocation MUST NOT spawn another `/batch` — that's how runaways happen.  Skill validates the task description doesn't contain "/batch".

## Decomposition rules

The decomposition LLM call should produce units that satisfy:

- **Independence**: changing unit A must not require unit B to land first.
- **Verifiability**: the unit description must include "verification: <how to check this worked>" (typically a pytest invocation or grep assertion).
- **Self-contained**: the unit's prompt to its subagent must be readable without the parent task — subagents see only their unit, not the original ask.

## Example invocation

```
/batch rename `get_provider()` to `resolve_provider()` across all 50 plugin extensions
```

Expected behavior:
1. Skill scans `extensions/*/` for `get_provider(` call sites → produces 50 units.
2. Prints the list, asks confirmation.
3. Spawns 30 subagents in parallel, then 20 more as the first batch finishes.
4. Each subagent opens its own PR like `rename get_provider→resolve_provider in extensions/foo`.
5. Aggregate report lists 50 PR URLs.

## What this skill refuses

- Changes that span >50 units in one invocation.  Split into batches of 30-50 yourself; the skill rejects bigger Ns to protect CI.
- Changes that aren't atomic (e.g. "implement feature X across the codebase" — that's a feature, not a batch migration).
- Spawning batches inside batches.

## Cleanup

When the run finishes (success or failure), the operator can:
- `gh pr list --label batch-<run-id>` to see all opened PRs.
- `oc worktrees prune` to remove any orphaned trees.
- `oc worktrees list` to inspect remaining persistent worktrees.

## Implementation pointer

Plumbing module: `opencomputer.agent.batch_orchestrator` (helper for unit
decomposition + parallel spawn coordination).  See its docstring for the
exact spawn loop + cap enforcement.
