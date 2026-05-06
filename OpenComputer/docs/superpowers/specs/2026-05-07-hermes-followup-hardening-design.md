# Hermes Deep Comparison — follow-up hardening (2026-05-07)

**Date:** 2026-05-07
**Driver:** post-PR-#484 brutal-honesty audit (concerns 3–6).
**Branch:** continues `feat/hermes-deep-comparison-2026-05-06`.

## Context

PR #484 (the operational-hardening 4-pack) shipped `error_classifier`,
`retry_utils`, `usage_pricing`, `cli_insights cost`, and the Mem0 backend.
The brutal-honesty review surfaced six concerns; this follow-up closes 3, 4,
5, and the load-bearing piece of 6.

| # | Concern | Resolution |
|---|---|---|
| 3 | CI not verified green | Rebased branch onto current `origin/main`; remaining red is pre-existing `social-traces` failures from a parallel session, NOT this PR. |
| 4 | No end-to-end runtime verification | `tests/test_loop_records_llm_call.py` drives `AgentLoop._run_one_step` with a stub provider and asserts `llm_calls` rows + non-empty provider name. Plus a live python smoke test confirmed `record_call_from_usage` produces `cost_usd=$0.061035` for known pricing. |
| 5 | Auxiliary call sites not recorded | Wired the highest-value site (compaction) via a `usage_recorder` callback. Other auxiliary sites (title-gen, judge, dreaming, recall, reasoning_summary, aux_llm) deferred — each is a separate constructor refactor. |
| 6 | Doc-flagged gaps untouched | This spec ships `oc skill publish` (the load-bearing missing skill-hub command) and a thin `path_safety.py` consolidator. Tirith expansion deferred — doc is unclear on detection-class deltas. |

## Scope of this spec

### Item A — `oc skill publish`

Today the skill hub ships `search` / `browse` / `inspect` / `install` /
`uninstall` / `installed` / `audit` / `update` / `tap-add` / `tap-remove` /
`tap-list`. The two doc-flagged gaps are `publish` and `snapshot`.

`snapshot` is dropping out of scope: there's no concrete consumer story —
"snapshot the active skill set" is a workflow nobody is currently asking
for. Easy to add later if demand surfaces.

`publish` IS load-bearing: a user who builds a useful skill locally has no
way to make it discoverable through the hub. The minimal flow:

```
oc skill publish <skill-dir>             # validates + prints next-step instructions
oc skill publish <skill-dir> --tap user/repo  # also pushes to a configured tap
```

Behaviour:
- Validate the skill dir's `SKILL.md` against `agentskills.io` standard
  (already shipped in `agentskills_validator.py`).
- Default mode (no `--tap`): print instructions for either committing the
  skill to a tap repo manually or running with `--tap`.
- `--tap user/repo` mode: requires the tap to already be registered
  (`oc skill tap-add` was run); the command writes/copies the skill into a
  local clone of the tap repo and prints the `git commit && git push`
  command for the user to execute. **We deliberately do NOT auto-push** —
  authorship + commit-message + signature are the user's call.

### Item B — `opencomputer/security/path_safety.py`

A small consolidator. Today path-validation logic is scattered:

- `tools/vision_analyze.py:is_safe_image_path` — refuses paths outside
  configured "safe roots."
- Various tool implementations sprinkle `Path.resolve().is_relative_to()`
  checks inline.
- `tools/bash_safety.py` enforces shell-command safety, not path safety.

This PR does NOT refactor every callsite (too large). It:

- Adds `opencomputer/security/path_safety.py` with two pure helpers:
  `is_safe_path(path, *, roots)` and `assert_safe_path(...)` (raises
  `UnsafePathError`).
- Migrates `tools/vision_analyze.is_safe_image_path` to delegate to the
  new helper (existing tests must continue to pass — regression-locked).
- Documents the pattern so new tool implementations have a canonical
  helper instead of rolling their own.

Future PRs migrate other inline checks. The win here is **one canonical
implementation** that can be hardened in one place.

## Out of scope (deferred)

- **Tirith detection-class expansion**: doc is unclear on what's missing
  in our wrapper vs the upstream binary. Deferred until a concrete
  "Tirith caught X but our scan missed it" example.
- **`oc skill snapshot`**: no concrete consumer story.
- **Other auxiliary call-sites for cost recording**: title-gen, judge,
  dreaming, etc. Each needs its own targeted refactor.
- **Path-safety migration of every existing inline check**: only
  `vision_analyze` migrated this PR; rest happens organically as
  contributors edit those files.

## Tests

- `tests/test_skill_publish.py` — validation passes/fails, `--tap`
  routing, dry-run prints expected instructions.
- `tests/test_path_safety.py` — table-driven over safe + unsafe paths
  including symlink traversal, `..`, absolute outside-root, NUL byte.
- Existing `tests/test_vision_analyze_image_path.py` tests still pass
  (regression-lock for the migration).

## Risks

| Risk | Mitigation |
|---|---|
| `path_safety` migration of `vision_analyze` changes behaviour | Existing test_vision_analyze_image_path.py is the ground truth; it must keep passing untouched. |
| `oc skill publish --tap` writes outside the user's tap clone | Validate the tap dir is a git repo before write; refuse if dirty + no `--force`. |
| User auto-pushes accidentally | Don't auto-push — print the command and let the user decide. |

## Self-review

- [x] No "TBD" placeholders.
- [x] Scope is single-PR-sized.
- [x] Each new module has a callsite (publish: cli_skills_hub; path_safety: vision_analyze).
- [x] Risks have explicit mitigations.
- [x] Out-of-scope items are explicit, not hand-waved.
