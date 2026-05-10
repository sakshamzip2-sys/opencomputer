# Kanban + Goals v2 Implementation Plan

**Goal:** Close the four real v2-spec gaps in the goal subsystem — strict-JSON judge with reason, UX banners with icons, configurable judge model + max_turns, mid-run race-guard.

**Architecture:** Single-PR, narrowly-scoped update to existing `agent/goal.py`, `agent/state.py`, `agent/loop.py`, `agent/config.py`, `cli_ui/slash_handlers.py`, `cli_goal.py`, `gateway/dispatch.py`, `cli.py`. Schema migration v13→v14 adds `goal_last_judge_reason` column. Kanban surface untouched (already complete).

**Spec:** `docs/superpowers/specs/2026-05-08-kanban-goals-v2-design.md`

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `opencomputer/agent/goal.py` | Modify | New `JudgeVerdict`; replace `judge_satisfied` with `judge_goal`; configurable judge routing |
| `opencomputer/agent/state.py` | Modify | `_migrate_v13_to_v14`; CRUD round-trips `last_judge_reason` |
| `opencomputer/agent/loop.py` | Modify | `_maybe_continue_goal` uses `judge_goal`, persists reason, fires banners via `goal_banner_callback` |
| `opencomputer/agent/config.py` | Modify | New `GoalsConfig` + `GoalJudgeConfig` + `AuxiliaryConfig` dataclasses |
| `opencomputer/cli_ui/slash_handlers.py` | Modify | Rich UX strings for `_handle_goal`; `is_running_agent` guard |
| `opencomputer/cli_ui/goal_banner.py` | Create | Pure formatter for `↻/✓/⏸` banners |
| `opencomputer/cli_goal.py` | Modify | Mirror UX strings + config-driven default budget |
| `opencomputer/gateway/dispatch.py` | Modify | `_active_runs` set + `_goal_midrun_check` |
| `opencomputer/cli.py` | Modify | Wire `goal_banner_callback` onto AgentLoop |
| `CHANGELOG.md` | Modify | Add v2 entry under Unreleased |
| Tests | Various | 68 new tests across 8 files |

## Tasks (all shipped on `feat/kanban-goals-v2-2026-05-08`)

| # | Task | Commit |
|---|---|---|
| 0 | Create feature branch | (HEAD setup) |
| 1+2 | Schema v14 migration + GoalState.last_judge_reason + SessionDB CRUD | `1b937a4b` feat(goal): schema v14 + GoalState.last_judge_reason + JudgeVerdict |
| 3+5+6+7 | JudgeVerdict + judge_goal strict-JSON + GoalsConfig/GoalJudgeConfig + provider routing | `b11a7ade` feat(goal): GoalsConfig + AuxiliaryConfig.goal_judge slots, judge_goal tests |
| 4 | `_maybe_continue_goal` uses `judge_goal`, persists reason, fires banner callback | `26c95463` feat(goal): _maybe_continue_goal uses judge_goal + persists reason + banner cb |
| 8 | `/goal` slash UX with icons, last_judge_reason surfacing, mid-run guard field | `ff26e274` feat(goal): /goal slash UX v2 — icons + reason + mid-run guard |
| 9 | `oc goal` CLI UX + config-driven default budget | `173807c1` feat(goal): oc goal CLI UX v2 — icons + reason + config-driven default budget |
| 10 | Banner formatter module + CLI input-loop wiring | `4b859a03` feat(goal): banner formatter + CLI input-loop wiring |
| 11 | Gateway `_active_runs` + `_goal_midrun_check` | `2378ece8` feat(goal): gateway refuses /goal <text> mid-run, allows control-plane |
| 12 | (Subsumed by Task 8 — `is_running_agent` field; CLI doesn't need wiring) | (no separate commit) |
| 13 | CHANGELOG entry | `a87c88ed` docs(changelog): Kanban + Goals v2 — Ralph-loop parity polish entry |
| 14 | Final test sweep + ruff + push + PR | (Task 14 commit — this set) |

## Test summary

- `tests/agent/test_state_goal.py` — 6 tests (migration + CRUD round-trip)
- `tests/agent/test_goal.py` — 17 tests (JudgeVerdict + JSON parse + fail-open + GoalState shape)
- `tests/test_agent_loop_goal.py` — 7 tests (continuation gate + banner callback)
- `tests/test_config_goals.py` — 6 tests (config slot wiring + default fallback)
- `tests/cli_ui/test_slash_goal.py` — 15 tests (UX + mid-run guard)
- `tests/cli_ui/test_goal_banners.py` — 4 tests (formatter pure-function)
- `tests/test_cli_goal.py` — 29 tests (CLI UX + JSON + config-driven budget)
- `tests/gateway/test_goal_midrun_guard.py` — 6 tests (active-runs guard)

90 tests total in this scope (68 new + 22 pre-existing CLI tests still passing).

## Scope cuts (explicit, documented)

- Gateway-side banner forwarding (reasons still visible via `/goal status`).
- Dashboard ✨ Specify button frontend (separate PR).
- `hermes` → `oc` env var rename (no value).
- Goal judge cost telemetry (future concern).

## Self-review

| Spec gap | Tasks |
|---|---|
| Gap A (strict-JSON judge + reason) | 3, 4 |
| Gap B (UX strings + banners) | 8, 9, 10 |
| Gap C (config slots) | 5, 6, 7 |
| Gap D (mid-run guard) | 11, 12 |

Type consistency confirmed: `JudgeVerdict(done, reason)` shape matches across goal.py, loop.py, slash_handlers.py, cli_goal.py, goal_banner.py. `GoalState.last_judge_reason: str | None` consistent across all CRUD paths. `_goal_midrun_check` signature stable across guard helper + dispatch invocation site.
