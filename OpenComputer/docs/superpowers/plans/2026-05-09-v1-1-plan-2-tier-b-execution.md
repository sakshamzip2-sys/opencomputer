# v1.1 Plan 2 — M8.1 ship (2026-05-09)

Status: shipped this PR (#532). Scope shrunk after parallel-session
collision discovery — see "Why this PR exists" below.
Origin: extends `2026-05-08-v1-1-plan-2-architecture-features.md` after the
brainstorm-phase audit pattern (PR #526 already shipped M5.1 + the first audit
pass at `2026-05-09-v1-1-plan-2-refined-execution.md`).

## Why this PR exists alongside the parallel session's PRs

PR #526 (parallel session) shipped M5.1 (`oc session checkpoints <id>`). I
opened #532 with a broader scope (M4.1, M4.2, M4.3, M4.4, M5.4, M8.1)
expecting the parallel session would only ship M5.1 + M7. While #532's CI
was running, the parallel session opened five more PRs (#527-#531) covering
M7 / M4.1+M4.2 / M4.3+M4.4 / M5.2+M5.3 / M5.4 — duplicating most of #532's
scope.

To avoid double-merging, I reverted #532's M4.1, M4.2, M4.3, M4.4, M5.4 work
and shipped only the **truly unique value-add: M8.1 (`type: prompt` settings
hooks)** — the one plan-2 item the parallel session left uncovered.

| Item | Owner | Status |
|---|---|---|
| M5.1 `oc session checkpoints <id>` | PR #526 | open |
| M7.1 + M7.2 path-glob rules + CLI | PR #527 | open |
| M4.1 + M4.2 `delegate(isolation=...)` | PR #528 | open |
| M4.3 + M4.4 SKILL.md `context: fork` + `tools:` | PR #529 | open |
| M5.2 + M5.3 per-prompt checkpoint + rewind picker | PR #530 | open |
| M5.4 `ExitPlanMode` `next_mode` proposal | PR #531 | open |
| **M8.1 `type: prompt` settings hooks** | **PR #532 (this)** | **open** |
| M8.2 `agent` hook type | — | deferred (depends on M4.1 soak) |
| M4.5 `oc worktrees prune` | — | already shipped (`oc worktrees clean`) |
| M8.3 `PostCompact` | — | already shipped (`HookEvent.AFTER_COMPACTION`) |

## 9-lens audit on M8.1 (the surviving scope)

1. **Assumption-check** — verified against `origin/main`:
   - `opencomputer/agent/aux_llm.py:106` exposes `complete_text(messages, system, max_tokens, model)` — the entry point the handler calls.
   - `opencomputer/agent/config_store.py:108` is the `_parse_hooks_block` site we extend with a sibling parser.
   - `HookContext.tool_call.name` / `tool_call.arguments` are the right fields to render (verified via `shell_handlers.py`).
   - `cli.py:596` `_register_settings_hooks` is the wiring entry point.

2. **Architecture stress** — edge cases handled:
   - Aux-LLM hangs → `asyncio.wait_for` with a 5s default times out, fail-open.
   - Aux-LLM raises (transient or otherwise) → caught, fail-open with WARN log.
   - User declares a `score`-mode hook but the LLM returns no number → fail-open.
   - User passes a 100KB tool arg → estimated-token check skips the LLM call entirely (no API spend).
   - Both prompt and command hooks for same event → both fire, in registration order (matches existing plugin/settings coexistence).

3. **Alternative dismissal**:
   - Considered: a Python entry-point in the YAML (`type: python_callable`) — rejected because we already have plugin-declared hooks for code paths.
   - Considered: thinking-mode auto-LLM that streams reasoning — rejected for v1 (token cost + complexity); the response is parsed greedy on first decision token.
   - Considered: caching responses by hash of the rendered context — rejected as YAGNI (most PreToolUse calls have unique args).

4. **Requirement gap**:
   - "User wants to ask aux-LLM whether a Bash command is dangerous" — covered by `returns: allow_block`.
   - "User wants risk-scored hook" — covered by `returns: score`.
   - "User wants to silence the hook on a specific tool" — `matcher: "Edit|Write"` regex covers it (matches existing command-hook contract).

5. **Composability**:
   - `HookPromptConfig` shares the matcher / event semantics with `HookCommandConfig` — UIs that already render command hooks render prompt hooks identically.
   - The same YAML block accepts both types side-by-side; no migration needed.
   - Aux-LLM fallback chain (configured at `Config.fallback_providers`) is inherited automatically because we go through `complete_text`.

6. **Scope honesty**:
   - `prompt_handlers.py`: ~200 LOC.
   - `config_store.py` extension: ~120 LOC.
   - `config.py` dataclass + Config field: ~50 LOC.
   - `cli.py` registration: ~25 LOC.
   - Tests: ~350 LOC across 22 cases.
   - **Total: ~750 LOC.** Honestly 1-PR scope.

7. **API surface drift**:
   - `HookCommandConfig` untouched. New `HookPromptConfig` is purely additive.
   - `Config.prompt_hooks: tuple[HookPromptConfig, ...] = ()` defaults to empty — old configs parse unchanged.
   - `_parse_hooks_block` change is a one-line semantic — `type: prompt` is now silently skipped (parsed elsewhere) instead of emitting a warning.
   - Test for the warn-and-skip path updated to use `type: embedding` so the branch is still exercised.

8. **Failure modes** (documented per item):
   - LLM timeout → log + pass.
   - LLM error → log + pass.
   - Token cap exceeded → log + pass (no LLM call).
   - Ambiguous response → log + pass.
   - Score mode + no numeric in response → log + pass.
   - Invalid frontmatter (missing `system`, unknown `returns`, etc.) → skipped at parse time with WARN.

9. **YAGNI sweep**:
   - No streaming response parsing (greedy on first decision token suffices).
   - No response caching (most calls are unique).
   - No `agent: <template>` field on prompt hooks (that's M8.2's surface).
   - No multi-LLM ensemble (one provider; users can compose by writing two hooks).

## Acceptance gates met

```
pytest tests/test_prompt_hook_v1_1.py
# 22 new tests pass

pytest tests/test_settings_hooks.py
# 19/19 (after the type-skip semantics update)

ruff check on every touched file
# clean
```

## Files in this PR

- new: `opencomputer/hooks/prompt_handlers.py`
- new: `tests/test_prompt_hook_v1_1.py`
- modified: `opencomputer/agent/config.py` (HookPromptConfig + Config.prompt_hooks)
- modified: `opencomputer/agent/config_store.py` (`_parse_prompt_hooks_block` + `_parse_hooks_block` `prompt`-skip)
- modified: `opencomputer/cli.py` (`_register_settings_hooks` extension)
- modified: `tests/test_settings_hooks.py` (semantic update for valid `type: prompt`)
- new: `docs/superpowers/plans/2026-05-09-v1-1-plan-2-tier-b-execution.md` (this doc)
- modified: `CHANGELOG.md`

## What this refined plan refuses

- Re-shipping M4.1, M4.2, M4.3, M4.4, M5.4 (covered by parallel session PRs).
- Re-implementing M4.5 / M8.3 (already on main).
- Pre-shipping M8.2 `agent` hook type (depends on M4.1 from PR #528 having
  soak time on main first).
- Treating LLM-hook responses as authoritative under failure — fail-open
  is the contract; risk-rating hooks are advisory.
