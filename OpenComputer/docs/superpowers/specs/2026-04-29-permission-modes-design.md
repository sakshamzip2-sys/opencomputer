# Plan v2 — Permission Modes: rename `yolo` → `auto`, add `accept-edits`, Shift+Tab cycling, mode badge

## Context

The OpenComputer coding harness ([repo at `OpenComputer/`](OpenComputer/)) currently has two orthogonal mode booleans on `RuntimeContext`: `plan_mode` (read-only — destructive tools refused via [`plan_block.py`](OpenComputer/extensions/coding-harness/hooks/plan_block.py)) and `yolo_mode` (skip per-action confirmation; F1 ConsentGate set to pass-through). They're set at startup from CLI flags (`--plan`, `--yolo`) and toggled mid-session via two slash commands (`/plan` + `/plan-off` in [extensions/coding-harness/slash_commands/plan.py](OpenComputer/extensions/coding-harness/slash_commands/plan.py); `/yolo` in [opencomputer/agent/slash_commands_impl/yolo_cmd.py](OpenComputer/agent/slash_commands_impl/yolo_cmd.py)) — both mutate `runtime.custom[...]` because `RuntimeContext` is frozen.

Anthropic's Claude Code (cloned locally at [sources/claude-code/](sources/claude-code/)) ships a richer mental model: a single permission-mode axis with `default` / `plan` / `accept-edits` / `bypassPermissions`, cycled live by **Shift+Tab** and shown as a persistent badge. The `accept-edits` middle ground (auto-approve Edit/Write but still ask for Bash/network) is the most-used Claude Code mode in practice and is currently missing here.

This plan:

1. Renames `yolo` → `auto` (semantic + naming parity with Claude Code; the system-reminder at the harness level already says "Auto Mode Active").
2. Introduces a single canonical `PermissionMode` enum (`default` / `plan` / `accept-edits` / `auto`) without removing the existing two booleans (backwards compat).
3. Resolves "what mode is active right now?" through a single helper, `effective_permission_mode(runtime)`, that bridges CLI-set frozen fields and slash-command-set `runtime.custom` keys — fixing a pre-existing gap where `/plan` set `custom["plan_mode"]` but [plan_block.py:35](OpenComputer/extensions/coding-harness/hooks/plan_block.py) only read the frozen field, so the hard-block didn't trigger from `/plan`.
4. Adds the missing middle mode `accept-edits` (auto-approve Edit/Write/MultiEdit/NotebookEdit only — Bash and network keep prompting).
5. Adds the in-session affordances Claude Code has but we don't: **Shift+Tab** mode cycling, persistent **mode badge** in the TUI footer (rendered as a `Window` row in the existing `HSplit` — *not* prompt_toolkit's `bottom_toolbar=`, which is `PromptSession`-only and crashes on the custom `Application` shipped in PR #266), unified `/mode <name>` slash command + `/auto` / `/accept-edits` shorthands.
6. Keeps output styles (explanatory, learning) explicitly out of scope — separate axis, separate plan.
7. Maintains backwards compatibility for one minor version (until v1.2 or four merge-weeks, whichever comes first): `--yolo`, `/yolo`, `runtime.yolo_mode`, `runtime.plan_mode`, `runtime.custom["yolo_session"]`, `runtime.custom["plan_mode"]` all keep working with deprecation warnings emitted at most once per session.

Outcome: cleaner mental model, smoother UX, parity with Claude Code's well-known shortcut, a missing middle mode becomes available, and an existing `/plan` enforcement gap closes as a side effect.

---

## Audit log — what changed from v1 → v2

The v1 of this plan had two BLOCKERs surfaced by an independent code-review pass and a fresh codebase grep. Recording here so the implementer doesn't make the same wrong assumptions:

- **v1 said**: slash commands toggle modes by `dataclasses.replace`'ing `RuntimeContext`. **Reality**: `RuntimeContext` is `frozen=True, slots=True` ([runtime_context.py:26](OpenComputer/plugin_sdk/runtime_context.py)); existing `/plan` and `/yolo` mutate the dict in `runtime.custom` ([plan.py:25](OpenComputer/extensions/coding-harness/slash_commands/plan.py), [yolo_cmd.py:62](OpenComputer/opencomputer/agent/slash_commands_impl/yolo_cmd.py)). v2 follows the existing pattern — write to `runtime.custom`, read through a helper.
- **v1 said**: deprecated `plan_mode` / `yolo_mode` properties on `RuntimeContext` keep all callers working. **Reality**: [`PromptBuilder.build()`](OpenComputer/opencomputer/agent/prompt_builder.py) (lines 219, 357) and [`build_with_memory()`](OpenComputer/opencomputer/agent/prompt_builder.py) take **explicit boolean kwargs**, not a `RuntimeContext`. The properties don't help that call site. v2 threads `permission_mode` through `PromptContext` + both build methods + the [loop.py:706](OpenComputer/opencomputer/agent/loop.py) caller.
- **v1 proposed**: snapshot v3→v4 migration. **Reality**: [snapshot/quick.py:41-48](OpenComputer/opencomputer/snapshot/quick.py) `QUICK_STATE_FILES` covers `sessions.db`, `config.yaml`, `.env`, etc. — `RuntimeContext` is per-process startup state and is NOT in any snapshot file. v2 drops the migration; the schema bump and shim were unnecessary work.
- **v1 missed**: [opencomputer/gateway/protocol_v2.py:84](OpenComputer/opencomputer/gateway/protocol_v2.py) carries `plan_mode: bool` over the IPC wire. v2 adds an optional `permission_mode: str` field; old wire clients keep working.
- **v1 missed**: [hooks/shell_handlers.py:77-78](OpenComputer/opencomputer/hooks/shell_handlers.py) emits `plan_mode` and `yolo_mode` in the shell-hook env-var blob. v2 also emits `permission_mode`.
- **v1 missed**: [tools/bash_safety.py:14](OpenComputer/opencomputer/tools/bash_safety.py), [extensions/coding-harness/modes/plan_mode.py:54](OpenComputer/extensions/coding-harness/modes/plan_mode.py), [tasks/runtime.py:230](OpenComputer/opencomputer/tasks/runtime.py), and the `chat` subcommand call at [cli.py:1530](OpenComputer/opencomputer/cli.py) — all read or set the legacy fields and need either pass-through compat or a new permission-mode read.
- **v1 said**: `bottom_toolbar=` on the Application. **Reality**: `Application` doesn't accept `bottom_toolbar`; that kwarg is `PromptSession`-only. v2 adds a `Window` row to the existing `HSplit` ([input_loop.py:672-684, 711-719](OpenComputer/opencomputer/cli_ui/input_loop.py)).
- **v1 missed**: there's a pre-existing **enforcement gap** — `/plan` slash sets `custom["plan_mode"] = True` but `plan_block.py` only reads `runtime.plan_mode`. So today `/plan` does NOT actually engage the hard-block hook. v2 closes this gap as a side effect of unifying read sites through `effective_permission_mode()`.

---

## Mode lattice

Single canonical answer per session: `effective_permission_mode(runtime: RuntimeContext) -> PermissionMode`.

| Mode | Read tools | Edit / Write / MultiEdit / NotebookEdit | Bash | Network (WebFetch / WebSearch) | F1 ConsentGate | `plan_block.py` hook |
|---|---|---|---|---|---|---|
| `default` (no flags) | allowed | ask (ConsentGate prompt) | ask | ask | active | inactive |
| `plan` (`--plan` / `/plan`) | allowed | refused | refused | refused | active | **active** (existing) |
| `accept-edits` (`--accept-edits` / `/accept-edits`) — **NEW** | allowed | **auto-approve** | ask | ask | active | inactive |
| `auto` (`--auto` / `/auto`, was `--yolo` / `/yolo`) | allowed | auto | auto | auto | pass-through | inactive |

`accept-edits` auto-approves these tool names exactly: `Edit`, `Write`, `MultiEdit`, `NotebookEdit`. Bash is **not** included even when it would mutate files (`sed -i`, `> path`, `tee`); the user model is "edits via the explicit Edit-family tools are auto-approved; arbitrary shell isn't." Document this in the prompt branch and the `/help` legend.

Resolution precedence in `effective_permission_mode()`, top wins:

```
1. runtime.custom["permission_mode"]           — canonical session-mutable key (new)
2. runtime.custom["plan_mode"]   == True       → PLAN (legacy from /plan)
   runtime.custom["yolo_session"] == True      → AUTO (legacy from /yolo)
3. runtime.permission_mode (frozen field)      — set by CLI on session start (new)
4. runtime.plan_mode  == True                  → PLAN (legacy CLI flag)
   runtime.yolo_mode  == True                  → AUTO (legacy CLI flag)
5. PermissionMode.DEFAULT
```

If both legacy keys are set in a malformed state, `plan` wins (matches existing CLI precedence at [cli.py:879](OpenComputer/opencomputer/cli.py)).

---

## Mutation model (the load-bearing decision)

`RuntimeContext` is `@dataclass(frozen=True, slots=True)` and is the same instance threaded everywhere — including the parent → subagent dispatch path via `DelegateTool._current_runtime` (a class-level reference). Existing slash commands take advantage of the fact that `frozen` does not prevent mutating items inside the `custom: dict[str, Any]` field — only the outer field binding is frozen.

**Therefore:**

- CLI flags set the frozen `permission_mode` field at startup. Read at field-precedence #3 above.
- `/mode <name>` and the shorthand slash commands (`/auto`, `/accept-edits`, plus existing `/plan`, `/plan-off`) write to `runtime.custom["permission_mode"]`. Read at precedence #1 above.
- Subagent inheritance is automatic: `DelegateTool.execute()` does `dataclasses.replace(self._current_runtime, delegation_depth=..., parent_messages=...)`, which preserves `custom` by reference (it's a single dict). When the parent toggles via `/auto`, the same dict is observed by any concurrently-running subagent that reads through the helper.
- We never call `dataclasses.replace(runtime, permission_mode=...)` from a slash command — that would orphan `DelegateTool._current_runtime` and break subagent inheritance.

This design choice MUST be respected by every reader and writer.

---

## File-by-file changes (slices — each independently shippable)

### Slice 1 — Add the enum, helper, and frozen field  ([plugin_sdk/runtime_context.py](OpenComputer/plugin_sdk/runtime_context.py))
- Add `class PermissionMode(StrEnum)` with values `"default"`, `"plan"`, `"accept-edits"`, `"auto"`.
- Add `permission_mode: PermissionMode = PermissionMode.DEFAULT` frozen field — alongside, not replacing, `plan_mode` and `yolo_mode`.
- Add `def effective_permission_mode(runtime: RuntimeContext) -> PermissionMode` exported from `plugin_sdk` (per the SDK contract rules in [plugin_sdk/CLAUDE.md](OpenComputer/plugin_sdk/CLAUDE.md), add to `__all__`).
- Re-export from `plugin_sdk/__init__.py`.
- Tests: enum membership, helper resolves all 8 precedence cases including conflict, frozen invariant intact.

### Slice 2 — CLI flags  ([opencomputer/cli.py](OpenComputer/opencomputer/cli.py), [opencomputer/cli_cron.py](OpenComputer/opencomputer/cli_cron.py))
- Add `--auto` and `--accept-edits` Typer options on `chat` ([cli.py:1530](OpenComputer/opencomputer/cli.py)), `code` ([cli.py:1552](OpenComputer/opencomputer/cli.py)), and `resume` ([cli.py:1604](OpenComputer/opencomputer/cli.py)). `chat` and `resume` don't currently have `--yolo` — add `--auto` (and a deprecated `--yolo` alias to match `code`).
- Resolve precedence at the `_run_chat_session` boundary ([cli.py:879](OpenComputer/opencomputer/cli.py)): `plan > auto > accept-edits > default`. Build the canonical `PermissionMode` value, then construct `RuntimeContext(plan_mode=plan, yolo_mode=auto, permission_mode=mode)` so all three remain consistent — old field readers still work.
- `--yolo` keeps its current behavior on `code` and `cli_cron.py:114` (deprecation warning on stderr; emitted at most once per process). At [cli_cron.py:133](OpenComputer/opencomputer/cli_cron.py), the `plan_mode=not yolo` precedence inversion is preserved by mapping `--auto` to the same expression. Add a regression test.
- Update startup banner ([cli.py:926-928](OpenComputer/opencomputer/cli.py)) to print one mode line in mode-specific colour: green `default`, blue `accept-edits`, yellow `plan`, red `auto`. Banner respects `NO_COLOR`.
- One-shot deprecation warning helper: emit per-process via a module-level set guard so `oc code --yolo` doesn't spam if the slash also fires.

### Slice 3 — Slash commands
- Rename [opencomputer/agent/slash_commands_impl/yolo_cmd.py](OpenComputer/opencomputer/agent/slash_commands_impl/yolo_cmd.py) → `auto_cmd.py`. Mutate `runtime.custom["permission_mode"]` (canonical) AND continue mutating `runtime.custom["yolo_session"]` (legacy) so the F1 ConsentGate keeps reading what it currently reads. Class renamed `YoloCommand` → `AutoCommand`; keep a `YoloCommand` thin subclass registered under `name = "yolo"` that prints a one-time deprecation line and forwards to `AutoCommand`.
- Add `opencomputer/agent/slash_commands_impl/mode_cmd.py` providing `/mode` (no-arg → echo current effective mode; arg → switch by enum name; unknown arg → print the lattice table). Validates against enum.
- Add shorthand commands that all reuse the same handler path:
  - `/auto` — toggle/set AUTO (already covered by renamed `auto_cmd.py`).
  - `/accept-edits` — set ACCEPT_EDITS (in the same file as `/mode` for cohesion). Hyphen names work — [slash_handlers.py:99-104](OpenComputer/opencomputer/cli_ui/slash_handlers.py) splits on whitespace and the existing `reload-mcp` registration proves kebab keys resolve.
  - `/plan` and `/plan-off` already exist in [extensions/coding-harness/slash_commands/plan.py](OpenComputer/extensions/coding-harness/slash_commands/plan.py); update them to ALSO write `custom["permission_mode"]` so the unified helper resolves correctly via precedence #1 instead of falling through to legacy keys.
- Register `/auto`, `/mode`, `/accept-edits` in [opencomputer/agent/slash_commands_impl/__init__.py](OpenComputer/opencomputer/agent/slash_commands_impl/__init__.py).

### Slice 4 — Prompt template  ([opencomputer/agent/prompts/base.j2](OpenComputer/opencomputer/agent/prompts/base.j2), [opencomputer/agent/prompt_builder.py](OpenComputer/opencomputer/agent/prompt_builder.py))
- Add `permission_mode: PermissionMode = PermissionMode.DEFAULT` to `PromptContext` ([prompt_builder.py:184-190](OpenComputer/opencomputer/agent/prompt_builder.py)).
- Thread a `permission_mode` kwarg through `PromptBuilder.build()` ([line 219](OpenComputer/opencomputer/agent/prompt_builder.py)) AND `build_with_memory()` ([lines 357, 386](OpenComputer/opencomputer/agent/prompt_builder.py)). Default it to `PermissionMode.DEFAULT` for backwards compat with callers that only pass booleans.
- At [loop.py:706](OpenComputer/opencomputer/agent/loop.py), call `effective_permission_mode(self._runtime)` and pass the result through.
- Replace the two-branch `{% if yolo_mode %}…{% else %}…` block in [base.j2:147-158](OpenComputer/opencomputer/agent/prompts/base.j2) with a four-branch dispatch on `permission_mode`. Each branch states explicitly what is auto-approved vs gated. Keep the legacy `{% if plan_mode %}` block intact for now — render-time precedence is permission_mode first.

### Slice 5 — Hooks + injection providers  ([extensions/coding-harness/hooks/](OpenComputer/extensions/coding-harness/hooks/), [extensions/coding-harness/modes/](OpenComputer/extensions/coding-harness/modes/))
- [`plan_block.py:35`](OpenComputer/extensions/coding-harness/hooks/plan_block.py): change predicate from `not ctx.runtime.plan_mode` to `effective_permission_mode(ctx.runtime) != PermissionMode.PLAN`. **This closes the pre-existing gap** where `/plan` slash didn't engage the hard-block.
- New `extensions/coding-harness/hooks/accept_edits_hook.py`: PreToolUse hook returning a `decision="approve"` `HookDecision` for tool names in `{"Edit", "Write", "MultiEdit", "NotebookEdit"}` when `effective_permission_mode(ctx.runtime) == PermissionMode.ACCEPT_EDITS`. Bash and network tools fall through to the normal consent path. Test that `Bash sed -i` is NOT auto-approved.
- New `extensions/coding-harness/modes/accept_edits_mode.py`: `DynamicInjectionProvider` sibling to [`modes/plan_mode.py`](OpenComputer/extensions/coding-harness/modes/plan_mode.py). Renders an injection block when mode is ACCEPT_EDITS so the model knows edits are unprompted.
- [`modes/plan_mode.py:54`](OpenComputer/extensions/coding-harness/modes/plan_mode.py): change `if not ctx.runtime.plan_mode` to read effective mode (gap-closer counterpart to plan_block.py).
- F1 ConsentGate: locate the file (search `class ConsentGate` or `consent_gate` symbol in `opencomputer/` or `plugin_sdk/`), update its trigger predicate from `runtime.yolo_mode or runtime.custom.get("yolo_session", False)` to `effective_permission_mode(runtime) == PermissionMode.AUTO`. **This must land in the same PR as Slice 1's helper** to avoid a stale-flag window where `--auto` doesn't actually pass-through.
- Plugin author docs: [opencomputer/skills/opencomputer-hook-authoring/SKILL.md](OpenComputer/opencomputer/skills/opencomputer-hook-authoring/SKILL.md) and [skills/opencomputer-hook-authoring/references/event-catalog.md](OpenComputer/opencomputer/skills/opencomputer-hook-authoring/references/event-catalog.md) — update lines that reference `plan_mode`/`yolo_mode` to point to the helper.

### Slice 6 — Adjacent reads to update for consistency
- [hooks/shell_handlers.py:77-78](OpenComputer/opencomputer/hooks/shell_handlers.py): emit `permission_mode` (string value) alongside `plan_mode`/`yolo_mode` in the env-var blob piped to settings-declared shell hooks. Old hook scripts still see the legacy keys.
- [tools/bash_safety.py:14](OpenComputer/opencomputer/tools/bash_safety.py): comment + any references to `plan_mode` — keep the existing logic; bash-safety only fires in `plan_mode` and that doesn't change for `accept-edits`.
- [opencomputer/gateway/protocol_v2.py:84](OpenComputer/opencomputer/gateway/protocol_v2.py): add an optional `permission_mode: str = "default"` field. Old wire clients omit it and decode fine; new server emits it. Bump protocol minor version.
- [tasks/runtime.py:230](OpenComputer/opencomputer/tasks/runtime.py), [cron/scheduler.py:204-205](OpenComputer/opencomputer/cron/scheduler.py), [tools/cron_tool.py:208](OpenComputer/opencomputer/tools/cron_tool.py): RuntimeContext construction sites — pass `permission_mode=PermissionMode.DEFAULT` explicitly (or accept the default).

### Slice 7 — TUI: keybinding + mode badge  ([opencomputer/cli_ui/input_loop.py](OpenComputer/opencomputer/cli_ui/input_loop.py))
- Add Shift+Tab handler in the custom `Application`'s `KeyBindings` block ([line 433+](OpenComputer/opencomputer/cli_ui/input_loop.py)) cycling DEFAULT → ACCEPT_EDITS → AUTO → PLAN → DEFAULT. Mutates `loop.runtime.custom["permission_mode"]` and triggers a redraw via the existing `app.invalidate()` pattern.
- **Add a Window row to the existing HSplit** ([line 672-684, 711-719](OpenComputer/opencomputer/cli_ui/input_loop.py)) — NOT prompt_toolkit's `bottom_toolbar=` (that's a `PromptSession` kwarg; `Application.__init__` doesn't accept it and would raise `TypeError` on construct). The new Window contains a single-line `FormattedTextControl` whose render function reads `effective_permission_mode(loop.runtime)` and emits styled text like `mode: <name>` plus a glyph (`[D]`/`[E]`/`[A]`/`[P]`) so it works with `NO_COLOR` and screen readers.
- TTY-less guard: when stdout isn't a tty (piped input mode), skip the badge row. Existing layout has a similar guard for `paste_hint_window`.
- `/help` ([opencomputer/cli_ui/slash.py](OpenComputer/opencomputer/cli_ui/slash.py)) gains a one-line legend: "Shift+Tab cycles permission modes (default → accept-edits → auto → plan)."

### Slice 8 — Documentation
- [README.md](OpenComputer/README.md): add a "Permission modes" section with the lattice table.
- [CHANGELOG.md](OpenComputer/CHANGELOG.md): one entry covering the rename, the new mode, the new flags + slash commands, and the deprecation timeline.
- [CLAUDE.md §7](OpenComputer/CLAUDE.md): update gotcha #7 (HookContext.runtime) with a pointer to `effective_permission_mode` as the read pattern.
- [plugin_sdk/CLAUDE.md](OpenComputer/plugin_sdk/CLAUDE.md): note `PermissionMode` and `effective_permission_mode` are part of the public contract.
- [docs/superpowers/specs/2026-04-29-permission-modes-design.md](OpenComputer/docs/superpowers/specs/) — copy this plan as the design doc with the spec-self-review tweaks per the brainstorming skill's checklist.

### Slice 9 — Tests
- Move [tests/tier2_slash/test_yolo_cmd.py](OpenComputer/tests/tier2_slash/test_yolo_cmd.py) → `test_auto_cmd.py`. Cases: `/auto on/off/status`, `/yolo` deprecation forward, both end up with `effective_permission_mode == AUTO`.
- New `tests/tier2_slash/test_mode_cmd.py`: `/mode`, `/mode plan`, `/mode auto`, `/mode accept-edits`, `/mode invalid`, plus `/accept-edits` shorthand.
- New `tests/test_permission_mode_enum.py`: enum identity, frozen-dataclass invariant, all 8 helper precedence cases (custom > legacy custom > field > legacy field > default; plan-vs-auto conflict resolves to plan).
- New `tests/coding_harness/test_accept_edits_hook.py`: PreToolUse for Edit/Write/MultiEdit/NotebookEdit auto-approves; Bash + WebFetch + WebSearch fall through to default; bash with `sed -i` does NOT get approved.
- New `tests/test_plan_block_gap_close.py`: regression test for the pre-existing gap — `/plan` slash followed by an Edit tool call gets refused (today it doesn't).
- New `tests/test_cli_flag_aliasing.py`: `--yolo` and `--auto` produce identical RuntimeContext; cron `--yolo` precedence inversion preserved.
- New `tests/tui/test_mode_badge.py`: simulate Shift+Tab; assert `effective_permission_mode` cycles through the 4 values; assert FormattedTextControl render emits the new mode.
- Update existing tests at [test_phase6a.py:194](OpenComputer/tests/test_phase6a.py), [test_phase6d.py:50](OpenComputer/tests/test_phase6d.py), [test_phase6f.py:52](OpenComputer/tests/test_phase6f.py), [test_phase10f.py:496-507](OpenComputer/tests/test_phase10f.py), [test_link_understanding.py:135](OpenComputer/tests/test_link_understanding.py) — RuntimeContext construction now optionally accepts `permission_mode`; legacy boolean asserts keep passing.
- Update [test_cli_oc_code.py:48-50](OpenComputer/tests/test_cli_oc_code.py) — `--yolo` still in `--help`, `--auto` and `--accept-edits` newly visible.
- Update [test_base_prompt_engineered.py:34](OpenComputer/tests/test_base_prompt_engineered.py) — assert one of the 4 mode branches always renders.
- CI assertion: a smoke test that grep-counts non-test reads of `plan_mode`/`yolo_mode` and fails the build if any new ones appear without an accompanying `# noqa: legacy-mode-read` justification. Stops drift back into scattered checks.

---

## Pre-flight checklist (before writing any code)

Run these commands and read the outputs first. Each landed verify:

1. `grep -rn "yolo_mode\|yolo_session\|YOLO" OpenComputer/ --include="*.py"` → confirm every match is on the file list above.
2. `grep -rn "plan_mode" OpenComputer/ --include="*.py"` → same.
3. `grep -rn "ConsentGate\|consent_gate" OpenComputer/` → locate F1's gate file.
4. `grep -rn "bottom_toolbar" OpenComputer/` → confirm not used elsewhere (and don't introduce it).
5. `grep -rn "runtime.custom\[" OpenComputer/` → locate every other custom-dict consumer that might want a permission-mode read.
6. Read the F1 ConsentGate file end-to-end. Identify its current predicate. Confirm Slice 5's update lands cleanly.
7. Run `pytest OpenComputer/ -k "yolo or plan_mode or permission" -q` → green baseline before edits.

---

## Stress-test scenarios (must all pass post-implementation)

1. **Cron `--yolo` regression**: `oc cron run job --yolo` and `oc cron run job --auto` produce equivalent runs. Today's `plan_mode=not yolo` precedence ([cli_cron.py:133](OpenComputer/opencomputer/cli_cron.py)) preserved.
2. **`/plan` enforcement gap closes**: in the TUI, `oc code` (default), then `/plan`, then ask the model to Edit a file → refused with the existing plan-block message. (Today, refusal does NOT happen.)
3. **`accept-edits` doesn't leak to Bash**: in `/mode accept-edits`, ask "edit foo.txt" — auto-approved, no prompt. Then ask "run `ls`" — Bash still prompts. Then ask "run `sed -i 's/x/y/' foo.txt`" — Bash still prompts (NOT auto-approved despite mutating a file).
4. **Subagent inheritance**: parent in `auto`, parent dispatches `delegate(subagent_task=…)`. The subagent's `effective_permission_mode` returns AUTO. Toggle parent to `default` mid-subagent: in-flight subagent keeps its current behaviour (frozen snapshot of `permission_mode` field) but reads the live `runtime.custom["permission_mode"]` (because the same dict instance is shared).
5. **Legacy slash backwards compat**: `/yolo on` still works, prints a one-line deprecation note, and produces `effective_permission_mode == AUTO`.
6. **Wire protocol compat**: an old wire client (omitting `permission_mode`) can still issue requests; gateway derives mode from the legacy `plan_mode` bool.
7. **TUI rendering**: badge visible in `oc code`. Shift+Tab cycles. `NO_COLOR=1 oc code` shows the badge with glyph and no ANSI. Piping `echo "/exit" | oc code` skips the badge cleanly.
8. **Banner consistency**: `oc code --auto` and `oc code --yolo` both print the red `auto mode` banner; `--yolo` adds a deprecation line above it.
9. **Mode swap mid-streaming**: while a tool is executing, press Shift+Tab. The current tool finishes under its existing decision; the next tool dispatch uses the new mode. (Permission checks happen at PreToolUse — atomic per call.)
10. **Channel-adapter slash routing**: from Telegram, send `/auto`, `/mode plan`. Track B adapters dispatch slash through the central registry (verify by reading the gateway dispatch path); the new commands resolve.

---

## Critical files to read before coding

(Already covered in Pre-flight checklist + slice file paths above. Bookmarks for the implementer:)

- [plugin_sdk/runtime_context.py](OpenComputer/plugin_sdk/runtime_context.py) — frozen dataclass conventions
- [opencomputer/cli.py](OpenComputer/opencomputer/cli.py) lines 545, 831, 879, 926, 1530, 1552, 1598-1601, 1653 — every Typer entry point
- [opencomputer/cli_cron.py:114, 133](OpenComputer/opencomputer/cli_cron.py) — cron precedence inversion
- [opencomputer/agent/loop.py:706, 743](OpenComputer/opencomputer/agent/loop.py) — prompt build call site + injection collection
- [opencomputer/agent/prompt_builder.py:184-260, 356-388](OpenComputer/opencomputer/agent/prompt_builder.py)
- [opencomputer/agent/prompts/base.j2:47, 147-158, 271](OpenComputer/opencomputer/agent/prompts/base.j2)
- [opencomputer/agent/slash_commands_impl/yolo_cmd.py](OpenComputer/opencomputer/agent/slash_commands_impl/yolo_cmd.py) — rename target
- [opencomputer/agent/slash_commands.py:135](OpenComputer/opencomputer/agent/slash_commands.py)
- [extensions/coding-harness/slash_commands/plan.py](OpenComputer/extensions/coding-harness/slash_commands/plan.py) — existing `/plan` pattern
- [extensions/coding-harness/hooks/plan_block.py](OpenComputer/extensions/coding-harness/hooks/plan_block.py)
- [extensions/coding-harness/modes/plan_mode.py](OpenComputer/extensions/coding-harness/modes/plan_mode.py)
- [opencomputer/cli_ui/input_loop.py:433-722](OpenComputer/opencomputer/cli_ui/input_loop.py) — TUI Application + KeyBindings
- [opencomputer/cli_ui/slash.py](OpenComputer/opencomputer/cli_ui/slash.py), [opencomputer/cli_ui/slash_handlers.py](OpenComputer/opencomputer/cli_ui/slash_handlers.py) — slash registry
- [opencomputer/gateway/protocol_v2.py:84](OpenComputer/opencomputer/gateway/protocol_v2.py)
- [opencomputer/hooks/shell_handlers.py:77-78](OpenComputer/opencomputer/hooks/shell_handlers.py)
- [sources/claude-code/CHANGELOG.md](sources/claude-code/CHANGELOG.md) (v2.0.43 entry — Anthropic's mode taxonomy reference)
- [sources/claude-code/examples/settings/settings-lax.json](sources/claude-code/examples/settings/settings-lax.json), `settings-strict.json`

---

## Risk register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | F1 ConsentGate keeps reading legacy `yolo_session` after Slice 1 ships, so `--auto` no-ops for the duration | BLOCKER | Slice 1 + ConsentGate update in same PR, asserted by integration test |
| 2 | `--auto` collides with an unrelated existing flag | HIGH | Verified in pre-flight grep — clean. Add CLI `--help` snapshot test |
| 3 | Mid-session mode swap during a tool call leaves the tool in an inconsistent state | MEDIUM | Permission checks fire at PreToolUse — atomic. Document in stress-test #9 |
| 4 | Subagent dispatch uses class-level `_current_runtime` reference; mode swap on parent doesn't propagate | LOW | Mutating `runtime.custom` on the shared instance propagates for free; no `dataclasses.replace` involved |
| 5 | `bottom_toolbar=` would crash the custom Application | HIGH | Use HSplit Window row pattern; explicit guard in code review |
| 6 | TUI keybinding collides with PR #266 thinking-dropdown bindings | MEDIUM | Verified: Shift+Tab not used. Add to custom Application KeyBindings, not legacy `build_prompt_session` |
| 7 | Existing `/plan` enforcement gap was load-bearing (some user expected `/plan` to be soft-only) | LOW | Closing it is the correct behaviour. Call out in CHANGELOG |
| 8 | Wire protocol bump breaks old clients | MEDIUM | New field is optional with default — backwards compatible |
| 9 | Deprecation warnings spam logs | LOW | Module-level once-per-process guard |
| 10 | Color-only badge unreadable for colorblind / NO_COLOR users | LOW | Add ASCII glyph alongside colour |

---

## PR slicing

To keep diffs reviewable:

- **PR-1** (must-ship-together): Slice 1 (enum + helper + field) + Slice 5 ConsentGate predicate flip + Slice 6 adjacent reads + Slice 9 helper/enum tests + Slice 8 README/CHANGELOG line. *Pure infrastructure; no UX change yet.*
- **PR-2**: Slice 2 (CLI flags) + Slice 3 (slash commands) + Slice 4 (prompt template + builder threading) + Slice 9 slash + builder tests. *In-session control surface goes live.*
- **PR-3**: Slice 5 accept-edits hook + accept_edits_mode injection provider + Slice 9 hook integration tests + plan-block gap-close regression test. *Net-new mode behaviour.*
- **PR-4**: Slice 7 (Shift+Tab + mode badge) + Slice 9 TUI test + `/help` legend update. *Polish / parity with Claude Code UX.*

Each PR independently green; PR-2 onward depends on PR-1.

---

## Out of scope (explicit deferrals — honest)

- **Output styles** (explanatory, learning) — separate axis. Will be a sibling design doc next.
- **Org-policy gate** for disabling `auto` (Claude Code's `permissions.disableBypassPermissionsMode: "disable"`) — useful for enterprise; not Phase 1.
- **Per-tool allow/deny lists** in `settings.json` (Claude Code's `settings-strict.json` style) — Phase 2.
- **Removing the deprecated `--yolo` / `/yolo` / `runtime.yolo_mode` / `runtime.custom["yolo_session"]` / `runtime.custom["plan_mode"]`** — keep until v1.2 or 4 merge-weeks past PR-1, whichever comes first. Then a clean-up PR. Tracked as a docs/CHANGELOG item.
- **Telemetry** for mode-switch events — none today; add when we add telemetry generally.

---

## Verification (end-to-end)

1. `pytest OpenComputer/tests -x -q` — full suite green, including all new tests.
2. `ruff check OpenComputer/` — lint clean.
3. The 10 stress-test scenarios above — each manually exercised in `oc code`.
4. `oc cron run` smoke for `--yolo` and `--auto` — produced jobs differ only in the deprecation log line.
5. `NO_COLOR=1 oc code` — banner + badge readable without ANSI.
6. Wire compat: stand up `oc gateway`, hit it with a stale client (omit `permission_mode`), assert no error and behaviour falls back to legacy `plan_mode`.
7. Follow-up CI grep guard catches regressions (any new direct read of `plan_mode`/`yolo_mode` outside the deprecated/legacy path fails the build).
