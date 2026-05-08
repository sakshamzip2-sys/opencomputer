# Kanban + Goals v2 — Closing the Real Gaps

**Date:** 2026-05-08
**Status:** Design — shipped on `feat/kanban-goals-v2-2026-05-08`
**Source spec:** `~/Downloads/hermes-kanban-goals-v2.md` (Hermes reference, supplied by user)
**Companion:** `docs/refs/hermes-agent/2026-05-08-kanban-goals-execcode-hooks-parity.md`

## 1. Position

The Kanban surface (32+ CLI subcommands, 7 worker tools, 3 bundled skills, dashboard backend, multi-attempt history, workspaces, boards) is already complete in `opencomputer/kanban/` and `opencomputer/cli_goal.py`. Per the user's standing rule — "Only integrate something that actually makes sense. If you already have it, don't do it." — this PR is narrowly-scoped: close the four real v2 gaps the spec calls out.

## 2. Already shipped (verified, not re-implemented)

**Kanban surface:** 32 CLI subcommands (`opencomputer/kanban/cli.py`), 7 worker tools (`tools/_kanban_handlers.py`), 3 bundled skills (`opencomputer/skills/kanban-*/`), `/kanban` slash with bypass-running-guard + auto-subscribe, dashboard REST + WS backend, `task_runs` multi-attempt history, workspaces (scratch/dir:abs/worktree), per-project boards with slug validation + archive vs hard-delete, idempotency keys, `--max-runtime`, `--skill` pinning, `--tenant` scoping, forward-compat `workflow_template_id` / `current_step_key` columns, `gave_up` event after N spawn failures, `oc kanban daemon` deprecation, bulk `--summary` rejection, `oc kanban specify` (auxiliary-LLM expansion via PR #496).

**Goal surface (v1):** `/goal <text|status|pause|resume|clear>` slash, `oc goal set/status/pause/resume/clear` CLI, continuation gate in `agent/loop.py::_maybe_continue_goal`, fail-open judge, schema v11 persistence (text/active/turns_used/budget), continuation prompt as plain user-role message preserving prompt cache.

## 3. Real v2 gaps — this PR

### Gap A — Strict-JSON judge with `reason` field

**Spec:** `{"done": bool, "reason": "one-sentence rationale"}` + UX banners surfacing the reason.

**Before:** plain-text `SATISFIED`/`NOT_SATISFIED`, 8-token cap, rationale discarded.

**After:** `judge_satisfied` removed; `judge_goal()` returns frozen `JudgeVerdict(done, reason)`. Prompt switches to strict-JSON-only with explicit "no markdown fences" instruction. Parser strips ```` ```json ``` ```` fences before `json.loads`; fails open with self-explaining reason on `JSONDecodeError`, missing `done` key, network error, empty response. Reason persists via new `goal_last_judge_reason` column on `sessions`.

### Gap B — Goal UX strings with icons + reason

| Surface | New UX |
|---|---|
| Set | `⊙ Goal set ({budget}-turn budget): <text>` |
| Status (active) | `goal: <text>\n  status: active · turns N/{budget}\n  last judge: <reason>` |
| Status (budget exhausted) | `⏸ goal paused — N/{budget} turns used. Use /goal resume / /goal clear` |
| Achieved (loop banner) | `✓ Goal achieved: <reason>` |
| Continuation (loop banner) | `↻ Continuing toward goal ({turns}/{budget}): <reason>` |
| Pause / Resume / Clear | `⏸ goal paused.` / `↻ goal resumed.` / `✗ goal cleared.` |

Loop banners surface via `AgentLoop.goal_banner_callback`, formatter at `cli_ui/goal_banner.py::format_banner`. Wired on the CLI input loop; gateway-side forwarding deferred (reason still surfaces via `/goal status`).

### Gap C — Config slots

```yaml
goals:
  max_turns: 20

auxiliary:
  goal_judge:
    provider: openrouter
    model: google/gemini-3-flash-preview
```

`GoalsConfig.max_turns` (int, default 20) under top-level `Config`. `AuxiliaryConfig.goal_judge` (`provider: str | None`, `model: str | None`). `set_session_goal` resolves default budget from `default_config().goals.max_turns` when the kwarg is `None`. `_call_judge_model` routes through the configured provider when both fields are set, else falls back to `aux_llm.complete_text`.

### Gap D — Mid-run `/goal <new text>` race-guard

Set form races with the in-flight continuation prompt — refuse with `/stop first` hint. Status / pause / resume / clear remain unrestricted because they only touch control-plane state.

- **Gateway:** `Dispatch._goal_midrun_check(session_id, args)` returns refusal string when session_id is in the new `_active_runs: set[str]` (instrumented around the agent-loop call). Invoked from `_maybe_bypass_running_guard` BEFORE the bypass-flag gate so /goal is checked even though it isn't a bypass command.
- **CLI:** slash dispatch happens at turn boundaries by construction — no race to guard. `SlashContext.is_running_agent` field added with `lambda: False` default for parity with the gateway-side intent.

## 4. Out of scope (explicitly)

- **Dashboard ✨ Specify button frontend.** Backend endpoint exists; React source is in a separate vendored build pipeline. Filed as a separate PR.
- **`hermes` → `oc` env var renaming.** Existing `OC_KANBAN_*` names are correct for OpenComputer; no change.
- **Storage rewrite to a `state_meta` blob.** Schema v11+v14 columns work; rewriting to match spec wording would be destructive with zero user-visible benefit.
- **Goal judge cost telemetry.** Future concern; not blocking parity.

## 4.1 Initially deferred, since shipped (Task 14 follow-through)

- **Gateway banner forwarding.** Initially documented as a UX-only deferral in the first ship. Closed in the same PR via the per-session callback registry on `AgentLoop` (`set_goal_banner_callback` / `clear_goal_banner_callback`) and gateway-side `Dispatch._install_goal_banner_callback`, which uses `asyncio.run_coroutine_threadsafe` to schedule `adapter.send` against the event loop bound to the dispatch turn. Per-session keying isolates banners to the right chat when one AgentLoop serves multiple concurrent sessions on the same profile. The CLI's single global `goal_banner_callback` remains as the fallback so the existing CLI input-loop wiring stays untouched.

## 5. Schema migration

Schema v13 → v14 — additive nullable column on `sessions`:

```sql
ALTER TABLE sessions ADD COLUMN goal_last_judge_reason TEXT;
```

Idempotent (PRAGMA `table_info` check + ALTER if missing). Old rows read NULL → `GoalState.last_judge_reason = None`. `_self_heal_columns` table also updated (defense in depth).

## 6. Test strategy

68 new tests across:

- `tests/agent/test_goal.py` — `JudgeVerdict`, JSON parse, fence strip, fail-open paths
- `tests/agent/test_state_goal.py` — schema v13→v14 migration + idempotence + reason CRUD round-trip
- `tests/test_agent_loop_goal.py` — `_maybe_continue_goal` persists reason + fires banners
- `tests/test_config_goals.py` — config slot wiring + default fallback
- `tests/cli_ui/test_slash_goal.py` — UX strings + mid-run guard
- `tests/cli_ui/test_goal_banners.py` — banner formatter
- `tests/test_cli_goal.py` — CLI UX + `--json` field
- `tests/gateway/test_goal_midrun_guard.py` — gateway refuses set form

## 7. Implementation handoff

Plan: `docs/superpowers/plans/2026-05-08-kanban-goals-v2-plan.md` (14 tasks, all shipped).

Branch: `feat/kanban-goals-v2-2026-05-08`. Commits land sequentially per task.
