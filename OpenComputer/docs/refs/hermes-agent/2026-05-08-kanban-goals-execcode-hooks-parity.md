# Hermes Doc-2 Parity — Kanban + Goals + Code-Execution + Event Hooks

**Date:** 2026-05-08
**Status:** Ship-ready findings doc
**Source:** Two Hermes Agent reference docs supplied by the user verbatim:

1. *Hermes Agent — Kanban, Collaboration & Persistent Goals*
2. *Hermes Agent — Code Execution & Event Hooks*

**Companion specs (same author, same day):**
- `2026-05-08-hermes-doc-parity-design.md` — Quickstart / CLI / TUI / WSL2 / Configuration parity (already merged separately).
- `2026-05-08-hermes-gateway-cron-delegation-parity-design.md` — Messaging gateway / webhook / cron / delegation parity (PR #488 + a separate PR-3 in flight on parallel worktrees).

This doc closes the parity question for the two specific Hermes references named above. It is intentionally a **snapshot** — future deep-comparison docs supersede it.

---

## 1. Filter applied

The user's standing rule (verbatim, 2026-05-08): *"Only integrate something that actually makes sense. If you already have it, don't do it. If you're missing it, it doesn't mean that we should just fill it just because we're missing it. We will fill it because it makes sense."*

Then for THIS doc, the user also said: *"do all A b c d everything and also do this as well"* — i.e. ship the originally-parked items too. We honoured both: ship every gap, with a clear rationale per item that explains what we *would* have parked under the original filter so the next reader knows the intent behind each piece.

---

## 2. Gap analysis

### 2.1 Doc 1 (Kanban + Persistent Goals) — already shipped

| Hermes feature | OC equivalent | Code path |
|---|---|---|
| `hermes kanban` CLI subcommand surface (init/create/list/show/assign/link/unlink/comment/complete/block/unblock/archive/tail/dispatch/runs/notify-*/gc/boards/specify) | `oc kanban …` (32 subcommands — surface is broader than Hermes) | `opencomputer/kanban/cli.py` (~2,180 LOC) |
| Worker-side tools `kanban_show / complete / block / heartbeat / comment / create / link` | All 7 present | `opencomputer/tools/_kanban_handlers.py` |
| `kanban-worker` and `kanban-orchestrator` skills | Both bundled (+ a bonus `kanban-video-orchestrator`) | `opencomputer/skills/kanban-{worker,orchestrator}/` |
| `/kanban` slash command bypass-running-agent guard | `opencomputer/kanban/slash_command.py` with auto-subscribe to gateway notifications on `/kanban create` | — |
| Dashboard 6-column board (Triage / Todo / Ready / Running / Blocked / Done) with lane-by-profile + drag-drop | `opencomputer/dashboard/plugins/kanban/plugin_api.py` | — |
| `task_runs` table — multi-attempt history surfaced in worker context | Present | `opencomputer/kanban/db.py` |
| Workspace kinds: `scratch` / `dir:<path>` / `worktree` | Present | `opencomputer/kanban/workspace_payload.py` |
| Idempotency keys, max-runtime, skill pinning per-task | Present | `opencomputer/kanban/cli.py` `--idempotency-key` / `--max-runtime` / `--skill` flags |
| Boards (`oc kanban boards list/create/switch/rm`) | Present | `_cmd_boards` (`opencomputer/kanban/cli.py`) |
| Tenant scoping (`$HERMES_TENANT` ↔ OC equivalent) | `--tenant` flag on create / list, propagated to workers via env | — |
| `/goal <text>` slash command (Ralph loop) | `opencomputer/agent/goal.py` + slash dispatch | — |
| **Bonus over Hermes:** `oc goal set/status/pause/resume/clear` CLI surface | `opencomputer/cli_goal.py` | Hermes only ships the slash form |
| Standing Orders parser for `## Program: <name>` blocks in AGENTS.md | `opencomputer/agent/standing_orders.py` (full state-machine parser; the rev-1 regex variant was rejected in audit) | — |

### 2.2 Doc 1 — gaps closed in this PR

| Hermes feature | What we did | Why |
|---|---|---|
| **`hermes kanban specify <id>`** — auxiliary-LLM expansion of one-line triage idea into structured spec | `oc kanban specify [<ids>...] [--all] [--promote-to todo\|ready] [--json]` + dashboard `POST /api/plugins/kanban/tasks/{id}/specify` endpoint | Real UX win: load-bearing for the dashboard's ✨ Specify button workflow. ~250 LOC + 14 tests. |

### 2.3 Doc 2 (Event Hooks + Code Execution) — already shipped

| Hermes feature | OC equivalent |
|---|---|
| Plugin hooks: `pre/post_tool_call`, `pre/post_llm_call`, `on_session_start/end`, `subagent_stop`, `pre_gateway_dispatch`, `pre_approval_request`, `post_approval_response`, `transform_tool_result`, `transform_terminal_output` | All 14 present + 11 OC-only events (USER_PROMPT_SUBMIT, PRE/AFTER_COMPACT, BEFORE_PROMPT_BUILD, BEFORE_MESSAGE_WRITE, BEFORE_TASK, BEFORE_INSTALL, BEFORE_MODEL_RESOLVE, MESSAGE_SENDING/SENT) |
| Shell hooks (`hooks:` block in `config.yaml`, Claude-Code shape — exit-code 0/2 contract, `OPENCOMPUTER_*` env vars, `CLAUDE_PLUGIN_ROOT` alias) | Already shipped (CLAUDE.md III.6) |

### 2.4 Doc 2 — gaps closed in this PR

| Hermes feature | What we did | Why |
|---|---|---|
| **`on_session_finalize` plugin hook** — last-chance flush before surface tear-down (CLI exit, gateway evict, wire disconnect) | `HookEvent.SESSION_FINALIZE` + `opencomputer/hooks/session_lifecycle.py` helper + fire-points in `cli.py` REPL exit | OC's `SESSION_END` fires per ``run_conversation`` (every turn), not per surface tear-down. The two events have orthogonal use cases. |
| **`on_session_reset` plugin hook** — gateway-only, fires after `/new` / `/reset` allocates a new session id; previous id exposed for state carry-forward | `HookEvent.SESSION_RESET` + fire from `_on_clear` callback in `cli.py` | Originally I dropped this in the brainstorm audit (covered transitively by SESSION_END+SESSION_START) but the user explicitly asked for "everything", so it's in. The `previous_session_id` field is the piece that distinguishes it from a SESSION_START. |
| **`transform_llm_output` plugin hook** — rewrite the final assistant response before delivery; first non-empty rewrite wins | `HookEvent.TRANSFORM_LLM_OUTPUT` + blocking fire in `agent/loop.py` END_TURN return path | Real PII-redaction / tone-adjustment use case. DB persistence stays original (rewrite is "for delivery only" — symmetric with `TRANSFORM_TOOL_RESULT`). |
| **`execute_code` tool** — Python-via-Unix-socket-RPC sandbox; only `print()` enters context | Phase 3 of this PR — see §3 below | Substantial security + maintenance surface but the user asked for it. |
| **Gateway `~/.opencomputer/hooks/<name>/HOOK.yaml + handler.py` file-discovery** — drop-in startup hooks without writing a plugin | Phase 4 of this PR — see §4 below | OC already has plugin hooks AND shell hooks; the user asked for the third surface anyway. |
| **`BOOT.md` community pattern** — natural-language startup instructions that the gateway executes via a one-shot AIAgent | Phase 4 of this PR | Lightweight "drop a file, get init" pattern. Coexists with HOOK.yaml. |

---

## 3. Phase 3 — `execute_code` (this PR)

See `opencomputer/tools/execute_code/` (added in commit `feat(execute_code)` of this branch). Key invariants kept identical to Hermes:

* Two execution modes: `project` (default — session's working dir + active venv python) vs `strict` (temp staging dir + `sys.executable`). Identical security guarantees in both.
* Resource limits: 300s timeout, 50KB stdout, 10KB stderr, 50 tool calls per script. Configurable via `code_execution.timeout` / `code_execution.max_tool_calls`.
* Env scrub: variables containing `KEY` / `TOKEN` / `SECRET` / `PASSWORD` / `CREDENTIAL` / `PASSWD` / `AUTH` are stripped before subprocess spawn. Skills' `required_environment_variables` auto-pass through; manual passthrough via `terminal.env_passthrough`.
* Recursion guard: scripts cannot call `execute_code` recursively, `delegate_task`, or MCP tools.
* Linux/macOS only — disabled on Windows (falls back to sequential tool calls).

---

## 4. Phase 4 — Gateway HOOK.yaml + BOOT.md (this PR)

`~/.opencomputer/hooks/<name>/HOOK.yaml` declares which gateway events the directory's `handler.py` listens for:

```yaml
# ~/.opencomputer/hooks/log-startups/HOOK.yaml
events:
  - gateway:startup
  - session:start
  - session:end
```

```python
# ~/.opencomputer/hooks/log-startups/handler.py
async def handle(event_type: str, context: dict) -> None:
    print(f"[{event_type}] {context.get('session_id')}")
```

Discovery happens at gateway startup; handlers are async + exception-isolated. BOOT.md, when present at `~/.opencomputer/BOOT.md`, fires a one-shot AIAgent on `gateway:startup` to execute the natural-language instructions — using `[SILENT]` as the response if nothing needs attention.

Why this third hook surface coexists with plugin hooks + shell hooks:

* **Plugin hooks** require writing a plugin (manifest, register, plugin_sdk imports).
* **Shell hooks** in `config.yaml` shell out per call.
* **Gateway HOOK.yaml** is the in-between: drop a Python file with one async `handle()` function, get all gateway events without the plugin overhead.

Whether all three surfaces should consolidate long-term is an open design question; for this PR we ship the third surface to honour the user's explicit "do everything" instruction.

---

## 5. Out of scope (explicitly)

- New cron triggers for kanban (already covered by `oc cron` shipped earlier this week).
- `oc kanban specify --reformat` to re-run on already-spec'd tasks (deliberate refusal — `SpecifyError` raises on non-triage, manual edit otherwise; if a re-spec workflow becomes a real ask, add a `--force` flag).
- Goal-judge model swap to a dedicated cheap aux model (currently piggybacks on `aux_llm.complete_text` which already routes through whatever provider runs chat — re-add a `auxiliary.goal_judge.model` slot only when a user actually asks for it).

---

## 6. Validation

- `pytest tests/test_phase_doc2_hooks.py tests/test_kanban_specify.py tests/test_dashboard_kanban_specify.py` — all green.
- `ruff check` — clean across touched files.
- Existing hook tests (`test_hook_expansion.py` count assertion bumped 25 → 28) still pass.
- Full kanban suite still green.

---

## 7. Decision log

| Decision | Reasoning |
|---|---|
| Add 3 hook events even though `SESSION_RESET` is largely redundant | User asked for "everything" — and the `previous_session_id` field is genuinely orthogonal to the SESSION_END+SESSION_START fallback path. |
| Specify uses `aux_llm.complete_text` (no new config slot) | Mirrors `goal._call_judge_model` precedent — auxiliary calls inherit the user's chat provider rather than introducing a new model-config surface. |
| Specify command refuses non-triage tasks (loud) rather than silently overwriting | Catches accidental `specify <wrong_id>` mistakes at the cost of one error message. The dashboard maps this to HTTP 409 distinctly from 404 and 502. |
| Body length cap = 4000 chars | Defends DB against runaway model output. 4000 chars ≈ 800 tokens — comfortably more than any reasonable spec. |
| `execute_code` ships in its own subprocess module rather than re-using the existing terminal sandbox | Different security profile (Python interpreter vs shell) and different env scrubbing rules. Sharing code would couple two abstractions that drift apart. |
| Gateway HOOK.yaml uses synthetic-module-name imports per `CLAUDE.md` gotcha #1 | Avoids `sys.modules` collisions when multiple hook directories share `handler.py` filenames. |
