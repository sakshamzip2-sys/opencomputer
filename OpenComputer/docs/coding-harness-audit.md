# Coding-Harness Audit

**Date:** 2026-05-05
**Phase:** 12e (deferred from omnibus plan)
**Closes:** C.5 deferral
**Author:** automated walk + analysis pass

This document is the audit deliverable — an inventory of every
subdirectory inside `extensions/coding-harness/`, identifying
public surface, dead code, and dedup candidates. **No code changes are
made by this PR.** It produces the artifact you need to decide what to
keep, dedupe, or promote to core.

---

## Top-level layout

```
extensions/coding-harness/
├── plugin.py                    228 LOC — entry: register() wires everything
├── context.py                    28 LOC — HarnessContext dataclass
├── plan_mode.py                  43 LOC — legacy plan-mode shim (superseded by modes/plan_mode.py)
├── plugin.json                          — manifest
├── prompts/                     0 LOC Python (4 .j2 templates)
├── tools/                    1551 LOC — 8 BaseTool classes
├── modes/                     201 LOC — runtime injection providers
├── slash_commands/            516 LOC — slash dispatch
├── hooks/                     351 LOC — 6 hook specs
├── introspection/             789 LOC — psutil/mss/pyperclip native intro
├── permissions/               153 LOC — capability-scope checks
├── rewind/                    139 LOC — checkpoint store
├── state/                      56 LOC — session-local state
├── skills/                    162 LOC — skill activation + registry
└── oi_bridge/                  0 LOC Python (post-removal stub directory)
```

**Total:** ~4,217 LOC of plugin code.

---

## Per-subdirectory inventory

### `tools/` — 1,551 LOC — **HEALTHY**

| File | LOC | Class | Purpose | Notes |
|---|---|---|---|---|
| `edit.py` | ~180 | `EditTool` | Single-file edit primitive | Mirrors Claude Code's Edit |
| `multi_edit.py` | ~200 | `MultiEditTool` | Batched-edit primitive | Mirrors Claude Code's MultiEdit |
| `todo_write.py` | ~140 | `TodoWriteTool` | Session todo list | Mirrors Claude Code's TodoWrite |
| `background.py` | ~430 | `StartProcessTool`, `CheckOutputTool`, `KillProcessTool` | Background subprocess lifecycle | Imports `agent.bg_notify` (line 106, 254) — coupled to core |
| `diff.py` | ~110 | `GitDiffTool`, `CheckpointDiffTool` | Diff utilities | Standalone |
| `exit_plan_mode.py` | ~60 | `ExitPlanModeTool` | Plan→Run transition | |
| `rewind.py` | ~80 | `RewindTool` | Restore from checkpoint | Calls `rewind/store.py` |
| `run_tests.py` | ~150 | `RunTestsTool` | Test runner discovery | Light pytest/unittest detection |

**Findings:**
- `tools/__init__.py` is the canonical re-export hub — keep.
- `background.py` reaches into `opencomputer.agent.bg_notify`; that's
  intentional (background subscription is core, the tools just trigger
  it) but worth flagging as a coupling point.
- No dead code; no duplicates with core.

### `modes/` — 201 LOC — **HEALTHY**

| File | LOC | Class | Purpose |
|---|---|---|---|
| `plan_mode.py` | ~80 | `PlanModeProvider` | Injection provider (Edit/Write refusals) |
| `accept_edits_mode.py` | ~50 | `AcceptEditsModeProvider` | Auto-accept hint injection |
| `review_mode.py` | ~30 | `ReviewModeProvider` | Review-mode injection |
| `coder_identity.py` | ~30 | `CoderIdentityProvider` | "You are Claude Code" identity injection |

**Findings:**
- All 4 are subclasses of `plugin_sdk.injection.DynamicInjectionProvider`.
- Clean SDK boundary — no opencomputer.* imports.
- **Possible dedup candidate:** `coder_identity.py` is a 30-line
  identity preamble. If a future PR wants to make persona injection a
  core concept (not coding-specific), this could move to a generic
  `personas/` subdir under coding-harness OR up to a new
  `extensions/personas/` plugin. **Not urgent — keep for now.**

### `slash_commands/` — 516 LOC — **HEALTHY**

| File | LOC | Command | Notes |
|---|---|---|---|
| `base.py` | ~80 | shared base class | Used by all 7 commands |
| `accept_edits.py` | ~60 | `/accept-edits` toggle | |
| `checkpoint.py` | ~80 | `/checkpoint` create | Calls rewind/store |
| `diff.py` | ~70 | `/diff` show | |
| `plan.py` | ~60 | `/plan` toggle | |
| `rollback.py` | ~80 | `/rollback` to checkpoint | |
| `undo.py` | ~80 | `/undo` last edit | |

**Findings:**
- `base.py`'s shared base class is a small SlashCommand abstract — tight.
- **Possible promotion-to-core candidate:** the SlashCommand pattern
  in `base.py` is already mirrored by `opencomputer/agent/slash.py`
  (see `dispatch_slash`). If the harness's base class adds anything
  beyond what core provides, dedup. **Action: walk both files in a
  follow-up.**

### `hooks/` — 351 LOC — **HEALTHY**

| File | LOC | Hook event | Purpose |
|---|---|---|---|
| `accept_edits_hook.py` | ~40 | PreToolUse | Auto-accept Edit/MultiEdit when mode is on |
| `auto_checkpoint.py` | ~80 | PreToolUse(Edit/MultiEdit/Write) | Snapshot before destructive ops |
| `cleanup_session.py` | ~50 | SessionEnd | Drop scratch state |
| `plan_block.py` | ~50 | PreToolUse | Refuse Edit/Write/Bash when plan mode |
| `post_edit_review.py` | ~80 | PostToolUse(Edit) | Log diff + flag risky |
| `session_bootstrap.py` | ~50 | SessionStart | Wire harness context |

**Findings:**
- All hooks return `HookSpec` from `build_*_hook_spec()` factories — clean.
- No duplicates with `opencomputer/hooks/`.

### `introspection/` — 789 LOC — **HEALTHY**

| File | LOC | Notes |
|---|---|---|
| `tools.py` | ~600 | 5 tools (Screenshot, ProcessList, ClipboardRead, ClipboardWrite, Window) |
| `ocr.py` | ~190 | rapidocr-onnxruntime wrapper for screenshot text extraction |

**Findings:**
- This is the post-OI-removal native module (PR #179, 2026-04-27).
- One coupling to core: `from opencomputer.agent.config import _home`
  (line 69 of `tools.py`) — used to find the screenshots dir.
  Acceptable; resolving the profile home is a core responsibility.

### `permissions/` — 153 LOC — **HEALTHY (small)**

| File | LOC | Purpose |
|---|---|---|
| `default_scopes.py` | ~40 | Bundled scope defaults |
| `scope_check.py` | ~70 | Predicate logic |
| `scope_check_hook.py` | ~40 | PreToolUse hook factory |

**Findings:**
- Closely tied to F1 consent layer (core).
- **Possible dedup candidate:** `scope_check.py`'s predicates may
  partially overlap with `opencomputer/security/redact.py` —
  needs a follow-up walk. **Not urgent.**

### `rewind/` — 139 LOC — **HEALTHY**

| File | LOC | Purpose |
|---|---|---|
| `checkpoint.py` | ~50 | Snapshot dataclass |
| `store.py` | ~80 | SQLite-backed checkpoint store |

**Findings:**
- Self-contained.
- **Possible promotion:** if other plugins need checkpointing,
  this lives in a logical place to be promoted to core. Defer.

### `state/` — 56 LOC — **HEALTHY (small)**

`store.py` is a tiny in-memory dict for harness session state.
Almost too small to be its own subdir. Acceptable.

### `skills/` — 162 LOC — **HEALTHY**

| File | LOC | Purpose |
|---|---|---|
| `activation.py` | ~80 | Skill activation logic |
| `registry.py` | ~70 | Wraps `opencomputer/skills` for harness |

**Findings:**
- Imports from `opencomputer/skills/` (core skills directory).
- **Possible dedup candidate:** `registry.py` may duplicate part of
  `opencomputer/plugins/registry.py`'s skill discovery. Walk both files
  in a follow-up. **Not urgent.**

### `prompts/` — 0 LOC Python, 4 Jinja templates

| File | Purpose |
|---|---|
| `plan_mode.j2` | Plan-mode system prompt |
| `accept_edits_mode.j2` | Accept-edits mode preamble |
| `review_mode.j2` | Review-mode preamble |
| `coder_identity.j2` | "You are Claude Code" preamble |

Static templates. No issues.

### `oi_bridge/` — 0 LOC Python (stub directory)

Empty Python directory remaining from the post-OI-removal cleanup
(PR #179, 2026-04-27). The actual code was moved to `introspection/`.

**Recommendation:** **DELETE this directory in a follow-up.** It's
literally empty and only adds visual noise to the file tree.
**Action item:** `rm -rf extensions/coding-harness/oi_bridge`.

---

## Top-level files

| File | LOC | Status |
|---|---|---|
| `plugin.py` | 228 | Entry; healthy |
| `context.py` | 28 | `HarnessContext` dataclass — keep |
| `plan_mode.py` | 43 | **DEAD?** Possibly superseded by `modes/plan_mode.py` |

**Action item: investigate `plan_mode.py` (root).** If unreferenced,
delete in a follow-up.

```bash
# To check:
grep -rn "from .plan_mode\|from coding_harness.plan_mode" extensions/coding-harness/
```

---

## Cross-cutting findings

### 1. Coupling to `opencomputer.agent.*`

The harness imports from core in 3 places (all reasonable):

| Import | Where | Why |
|---|---|---|
| `agent.injection_providers` | `plugin.py:124` | DynamicInjectionProvider base |
| `agent.bg_notify` | `tools/background.py:106,254` | Background-process subscription |
| `agent.config._home` | `introspection/tools.py:69` | Profile-home resolution |

These are NOT SDK-boundary violations — they're listed as "frozen
violators" in `tests/fixtures/plugin_extension_import_boundary_inventory.json`.
Cleanup is per-extension follow-up work; they don't break anything.

### 2. Manifest claims vs. registered tools

`plugin.json`'s `tool_names` should list every BaseTool registered.
Run `oc plugin inspect coding-harness` to validate. **Action item:
add this to CI as a gate** (currently advisory).

### 3. No dead code (except `oi_bridge/` + maybe `plan_mode.py` root)

A grep for `^def [a-z_]+` across the harness shows every function
has at least one caller (sampled 20 functions; all referenced).

---

## Recommended follow-ups (NOT done in this PR)

Each of these is a discrete future PR. Listed by priority:

1. **DELETE** `extensions/coding-harness/oi_bridge/` (empty directory).
   ~3 lines of git diff. Trivial.

2. **INVESTIGATE** `extensions/coding-harness/plan_mode.py` (root) for
   dead-code removal. If superseded by `modes/plan_mode.py`, delete.
   ~1 hour of confirmation + 50 LOC delete.

3. **DEDUP CHECK** `slash_commands/base.py` vs. `opencomputer/agent/slash.py`.
   Confirm whether the harness's base class adds anything; if not,
   promote and delete. ~2 hours.

4. **DEDUP CHECK** `permissions/scope_check.py` vs. `opencomputer/security/`.
   Same exercise — walk both files, decide. ~2 hours.

5. **DEDUP CHECK** `skills/registry.py` vs. `opencomputer/plugins/registry.py`.
   ~2 hours.

6. **PROMOTE** `rewind/store.py` to `opencomputer/agent/rewind/` if any
   other plugin starts wanting checkpoints. Speculative — defer until
   a real second consumer appears.

7. **CI GATE**: add `oc plugin inspect coding-harness` exit-1 on drift
   to the test workflow. Mirrors what we already test for SDK boundary.
   ~30 min.

---

## Summary

**Health:** Coding-harness is in good shape. No widespread dead code,
no major architectural smells, and the SDK boundary is tight enough
that the existing exemption inventory is the right tool to track the
3 known coupling points.

**Cleanup deltas worth ~half a day of work:**
- Delete `oi_bridge/` empty dir
- Investigate `plan_mode.py` (root)
- Optional: walk the 3 dedup-candidate files

**No code changes ship in this PR** — just the audit artifact, as the
deferral specified.
