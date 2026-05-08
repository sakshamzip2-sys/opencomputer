# Hermes Doc-2 Residuals — Code Execution & Event Hooks Closeout

**Date:** 2026-05-08
**Status:** Spec — ready for plan
**Source:** `Hermes Agent — Code Execution & Event Hooks` reference doc (full version, supplied by user 2026-05-08)
**Companion:** `2026-05-08-kanban-goals-execcode-hooks-parity.md` (PR #496 findings doc, on main)

---

## 1. Problem statement

The user supplied the Hermes "Code Execution & Event Hooks" reference and instructed: *"do this follow this flow as well"*, with the superpowers brainstorm → audit → plan → audit → execute workflow.

PR #496 (`b6787ecd`, 2026-05-08) already shipped the load-bearing surface from this exact doc:

* `ExecuteCode` tool (Python subprocess + tool RPC + project/strict modes + recursion guard + env scrub + Linux/macOS only).
* Plugin hooks: 14 of the 15 Hermes events plus 11 OC-only events (25→28 after the PR).
* Gateway file-discovery hooks at `~/.opencomputer/hooks/<name>/{HOOK.yaml,handler.py}` with `command:*` wildcard.
* `BOOT.md` startup pattern.
* Shell hooks via `hooks:` block in `config.yaml` (Claude Code shape, exit-code 0/2 contract, `OPENCOMPUTER_*` env vars, `CLAUDE_PLUGIN_ROOT` alias).

Re-porting the doc verbatim would duplicate ~95% of work already on main. The user's standing rule (verbatim, 2026-05-08): *"Only integrate something that actually makes sense. If you already have it, don't do it."*

This spec answers: **what genuine residuals from the Hermes doc still pass the makes-sense filter, and how do we ship them?**

---

## 2. Gap analysis (Hermes doc → OpenComputer state)

### 2.1 Already shipped — re-confirmation

| Hermes spec | OC code path |
|---|---|
| `execute_code` tool — subprocess + RPC + project/strict | `opencomputer/tools/execute_code.py` (PR #496) |
| Recursion guard via `OC_EXECUTE_CODE_DEPTH` | Same file, `_RECURSION_GUARD_ENV` |
| Env scrub (KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/PASSWD/AUTH) | `tools/ptc.py` |
| 50KB stdout cap, 10KB stderr cap | `tools/ptc.py` `_MAX_STDOUT_BYTES = 50 * 1024`, `_MAX_STDERR_BYTES` |
| Plugin hooks: pre/post_tool_call → `PRE/POST_TOOL_USE` | `plugin_sdk/hooks.py` HookEvent |
| Plugin hooks: pre/post_llm_call, on_session_start/end/finalize/reset | All in `plugin_sdk/hooks.py` |
| Plugin hooks: subagent_stop, pre_gateway_dispatch, pre/post_approval_request/response | All present |
| Plugin hooks: transform_tool_result, transform_terminal_output, transform_llm_output | All present |
| Gateway HOOK.yaml + handler.py file-discovery | `opencomputer/gateway/event_hooks.py` |
| Gateway events `gateway:startup`, `session:start/end/reset`, `agent:start/step/end`, `command:*` | Same file + dispatch fire-points in `gateway/dispatch.py` |
| `command:*` wildcard | `event_hooks.GatewayHook.matches()` (line ~125) |
| `BOOT.md` one-shot agent on startup | `opencomputer/gateway/boot_md.py` |
| Shell hooks `hooks:` block in `config.yaml` | `opencomputer/hooks/shell_handlers.py` |
| Shell hook env contract (`OPENCOMPUTER_EVENT/TOOL_NAME/SESSION_ID/PROFILE_HOME` + `CLAUDE_PLUGIN_ROOT` alias) | `shell_handlers.py:_build_env` |
| `oc hooks list/clear/revoke` | `opencomputer/cli_hooks.py` |
| `oc hooks test --payload JSON` (dry-run only) | `cli_hooks.py:cmd_test` |

### 2.2 Verified residual gaps — ship in this PR

| Gap | Hermes spec | OC current state | Why ship | Effort |
|---|---|---|---|---|
| **G1.** `oc hooks test --execute` actually fires | `hermes hooks test <event>` invokes the engine | `cli_hooks.py:148` raises "not yet implemented" | Without this, `oc hooks test` only enumerates handlers — can't reproduce "why didn't my hook fire" | ~30 LOC |
| **G2.** `oc hooks doctor` | `hermes hooks doctor` — exec bit, allowlist, mtime drift, JSON validity, timing | No equivalent | Operability — `list` shows registration but not health | ~120 LOC |
| **G3.** Shell-hook stdout JSON wire protocol | Hermes accepts both `{"action":"block","message":"..."}` and `{"decision":"block","reason":"..."}` on stdout | OC parses only exit-code (0=pass, 2=block w/ stderr) | Hermes-authored hook scripts that return JSON would silently fail-open in OC; closing this lets users drop Hermes scripts in unchanged | ~60 LOC |
| **G4.** Shell-hook `{"context":"..."}` injection on PRE_LLM_CALL | Hermes reads stdout JSON; if `{"context":"..."}` is present and event is pre_llm_call, append to user message | OC's shell hooks can return only `pass`/`block`; cannot inject context | Lets a 5-line bash script inject git status / time / cwd without writing a Python plugin | ~50 LOC |
| **G5.** `code_execution.max_tool_calls` config + enforcement | Hermes spec: 50 RPC calls per script, configurable | OC has stdout cap + timeout + recursion guard, but no per-script call cap; one tool-call-loop bug in agent script could exhaust quota | ~50 LOC |

Total: ~310 LOC + ~25 tests across 5 commits.

### 2.3 Verified missing — explicitly NOT shipping (with reopen triggers)

| Hermes feature | Why park |
|---|---|
| **Shell-hook allowlist + per-`(event, command)` consent prompt + `~/.opencomputer/shell-hooks-allowlist.json`** | OC's design says editing `config.yaml` IS consent; the allowlist adds a parallel trust boundary that duplicates the existing one. ~200 LOC of UX (prompt, persist, doctor mtime drift, three bypass mechanisms). **Reopen if:** a user reports a real "I didn't realize I shipped a hook" incident, OR if we add support for hook autodiscovery from drop-in directories (where consent IS implicit and the prompt would be load-bearing). |
| **`hermes_tools` module name in execute_code prologue** | OC tool stubs are PascalCase by convention (Read/Write/Edit/Grep/Glob/WebFetch/WebSearch/Bash) and inserted as bare names, not import-able. Adding `from hermes_tools import web_search` aliases is pure sugar. **Reopen if:** a user pastes a Hermes execute_code script and it fails to run unmodified (real-world cross-port pain). |
| **`hermes hooks revoke <command>` (allowlist removal)** | Tied to allowlist (not shipping). OC's `oc hooks revoke <plugin_id>` already disables a plugin's hooks via `settings.local.json` — different semantics, different problem space. |
| **JSON wire protocol for `pre_gateway_dispatch` (skip / rewrite / allow)** | OC's plugin hooks already implement the equivalent (return `{"action": "skip", "reason": "..."}` from PRE_GATEWAY_DISPATCH). The Hermes spec's `pre_gateway_dispatch` example is a Python plugin hook example — already covered. Shell-hook variant is theoretical; gateway dispatch is the wrong place for shell-out. |

### 2.4 Why these five pass the makes-sense filter

* **G1 + G2** are debug/operability surfaces. Without them, "why didn't my hook fire" requires reading `hooks/engine.py`. Cost ≪ value.
* **G3 + G4** close protocol parity for shell hooks. Concrete user value: a user porting Hermes scripts gets working OC behavior. Cost ~110 LOC, additive (exit-code path preserved as fallback).
* **G5** completes the documented `code_execution` config surface. Currently missing creates a footgun (one infinite-loop bug in agent script could silently exhaust the user's API quota — recursion guard catches recursion but not loops). Cost ~50 LOC.

---

## 3. Design

### 3.1 G1 — `oc hooks test --execute`

**Surface:**
```bash
oc hooks test EVENT [--payload JSON] [--for-tool NAME] [--execute]
```

**Behavior:**
* `--execute` flag toggles dry-run → real dispatch.
* Builds a synthetic `HookContext` from `--payload` JSON, the event name, and (for `Pre/PostToolUse`) `--for-tool` to populate `tool_call.name`.
* Invokes `engine.fire_blocking(ctx)` for blocking events (PRE_TOOL_USE, PRE_LLM_CALL, PRE_GATEWAY_DISPATCH, PRE_APPROVAL_REQUEST), `engine.fire(ctx)` (fire-and-forget) for the rest.
* Reports each handler's decision (or "no decision / fire-and-forget"), summary, and timing.

**Edge cases:**
* `--for-tool` provided but event is not Pre/PostToolUse → warning, run anyway with empty tool_call.
* Engine has no handlers for event → print "no handlers registered" and exit 0.
* Handler raises → engine swallows + logs (existing); CLI surfaces "raised: <ExceptionClass>".

### 3.2 G2 — `oc hooks doctor`

**Surface:**
```bash
oc hooks doctor [--json]
```

**Checks (one row per row in output table, with severity OK / WARN / ERROR):**

1. **HookEvent enum coverage** — every declared event has at least one fire-point in production code? (Best-effort grep over `opencomputer/`; missing fire-points → WARN with location pointer to where it'd live.)
2. **Plugin hooks registration** — `engine._hooks` dict count per event vs `ALL_HOOK_EVENTS`.
3. **Settings hooks** — `config.yaml` `hooks:` block: each command's resolved exec bit (if absolute path), file mtime (drift since last `oc hooks list`), JSON-parseable HOOK.yaml when applicable.
4. **Gateway file-discovery hooks** — every `~/.opencomputer/hooks/<name>/`: HOOK.yaml present + parses, handler.py present + has `handle` callable, valid event names. Stat any handler.py mtime. Synthetic-import to detect import errors without registering.
5. **`hooks_history` recent activity** — last fire across all events; warn if no fire in 24h on a busy profile (possible silent hook breakage).
6. **`hooks_auto_accept` / `OPENCOMPUTER_ACCEPT_HOOKS`** — note that OC has no allowlist (intentional design), and the env-var/setting are no-ops; print as INFO so users coming from Hermes know.

**Output**: Rich table with columns Severity / Check / Detail. `--json` returns a flat list for programmatic consumption.

### 3.3 G3 — Shell-hook stdout JSON wire protocol

**Current contract** (`opencomputer/hooks/shell_handlers.py`):
```
exit 0  → decision="pass"
exit 2  → decision="block", reason=stderr
other   → log warning, decision="pass" (fail-open)
```

**Augmented contract** (this PR):
```
1. Read stdout. If non-empty, attempt json.loads.
2. If valid JSON object, parse:
   - {"action": "block", "message": "..."}      → decision="block", reason=message  (Hermes canonical)
   - {"decision": "block", "reason": "..."}     → decision="block", reason=reason   (Claude Code)
   - {"action": "approve" | "allow"}            → decision="pass"                   (explicit pass)
   - {"decision": "approve"}                    → decision="pass"
   - {"context": "..."}                         → context-injection (only on PRE_LLM_CALL — see G4)
   - empty object {} or recognized null shape   → decision="pass"
   - unrecognized keys                          → decision="pass" (log INFO)
3. If JSON parse fails OR stdout empty: fall back to exit-code path (existing behavior).
```

**Precedence rule when both stdout JSON AND exit code are present:**
* Valid stdout JSON wins. Exit code is ignored unless JSON parse fails.
* Rationale: if a script prints `{"decision": "block"}` and exits 0, the user's intent is "block, and I exited cleanly". Trusting stdout is consistent with Hermes; consistent with Claude Code (which also reads stdout JSON when present per its own stdout-protocol fields).

**Backward compatibility:**
* All existing OC shell hooks emit empty stdout (printf '{}' or no output). They hit the exit-code fallback and behave identically.
* Tests for the existing 5 shell-hook test files stay green.

### 3.4 G4 — Shell-hook `{"context":"..."}` injection on PRE_LLM_CALL

**The problem this solves:** A user wants to inject git status into every turn. Today the only solution is writing a Python plugin with a `DynamicInjectionProvider` or a `register_hook("PreLLMCall", ...)` callback. With G4, a 5-line bash script suffices:

```bash
#!/usr/bin/env bash
# ~/.opencomputer/agent-hooks/inject-git-status.sh
cat - >/dev/null
status=$(git status --porcelain 2>/dev/null) && [[ -n "$status" ]] \
    && jq --null-input --arg s "$status" '{context: ("Uncommitted changes:\n" + $s)}' \
    || printf '{}\n'
```

**Mechanism:**
* `make_shell_hook_handler` returns `HookDecision(decision="pass", inject_context=text)` when stdout JSON has `{"context": "..."}` AND the event is PRE_LLM_CALL.
* Add `inject_context: str | None = None` field to `HookDecision` (additive — existing handlers unchanged).
* `agent/loop.py:_fire_pre_llm_call` already calls `engine.fire_blocking` for PRE_LLM_CALL — change it to also collect non-None `inject_context` strings, join with double newlines (Hermes spec), and append to the user message in the same way the InjectionEngine already does for plugin DynamicInjectionProvider results.
* For non-PRE_LLM_CALL events, `{"context": "..."}` is ignored with a debug log (don't error — Hermes spec also ignores).

**Why this is small:** the InjectionEngine already does most of the work. We only need to plumb shell-hook stdout context through `HookDecision` to the same join-and-append point.

### 3.5 G5 — `code_execution.max_tool_calls` config + enforcement

**New config slot** (in `agent/config.py`):
```python
@dataclass
class CodeExecutionConfig:
    timeout_seconds: float = 300.0
    max_tool_calls: int = 50              # NEW — Hermes parity
    terminal: dict[str, Any] = field(default_factory=dict)  # existing
```

**Enforcement** (in `tools/ptc.py`):
* Track per-call counter in the RPC loop. Increment before dispatch.
* If counter > max → return `{"error": "tool_call_limit_exceeded", "limit": <N>}` to the script, which the stub raises as `RuntimeError`.
* Surface the cap as a clean error in the parent `ToolResult` (not a timeout — different failure mode).
* Default 50 (Hermes default). Configurable via `code_execution.max_tool_calls` in `config.yaml`.

**Why this is needed:** the script's tool-call loop is currently bounded only by the 300s timeout. A buggy script doing `while True: read_file(x)` would consume both API quota and child-process CPU until timeout. Cap closes the footgun.

### 3.6 Doc surface update

**Touch:**
* `OpenComputer/CLAUDE.md` III.6 — add the augmented stdout JSON contract (current copy says only exit-code-based; add the JSON shapes + precedence rule).
* `OpenComputer/docs/refs/hermes-agent/2026-05-08-kanban-goals-execcode-hooks-parity.md` — add a §2.5 "follow-up residuals shipped in PR #<NNN>" pointing at this spec.

---

## 4. Implementation plan — 1 PR, 6 commits

| # | Commit | Surface |
|---|---|---|
| 1 | `feat(hooks): G1 — oc hooks test --execute fires synthetic events` | `cli_hooks.py:cmd_test` body + 4 tests |
| 2 | `feat(hooks): G2 — oc hooks doctor health diagnostics` | new `cli_hooks.py:cmd_doctor` + 6 tests |
| 3 | `feat(hooks): G3 — shell-hook stdout JSON wire protocol (Hermes + CC shapes)` | `shell_handlers.py` parse + dispatch + 6 tests |
| 4 | `feat(hooks): G4 — shell-hook context injection on PRE_LLM_CALL` | `HookDecision.inject_context` + `loop.py:_fire_pre_llm_call` + 4 tests |
| 5 | `feat(execute_code): G5 — code_execution.max_tool_calls cap` | `agent/config.py:CodeExecutionConfig` + `tools/ptc.py` enforcement + 4 tests |
| 6 | `docs(hooks): augmented JSON protocol + residual followup note` | `CLAUDE.md` III.6 + parity findings doc |

### Test strategy

* **G1**: real engine, register a stub handler, fire via CLI, verify dispatch + result.
* **G2**: pre-populated profile dir with valid + broken HOOK.yaml + non-exec command + valid command + recent fire-history → verify each row's severity.
* **G3**: parametrized over both JSON shapes × {block, pass, no-op}, plus malformed-JSON + empty-stdout fallback to exit-code path.
* **G4**: shell hook returning `{"context": "..."}` on PRE_LLM_CALL → verify injected into next user message; same return on POST_TOOL_USE → verify ignored.
* **G5**: synthetic script with 51 stub-call loop → verify error surfaced, limit honoured; config override to 5 → verify lower limit honoured.

### Validation gates

* `pytest tests/test_cli_hooks.py tests/test_settings_hooks.py tests/test_phase_doc2_hooks.py` green.
* `pytest tests/test_pr8_exec_trace_and_bus_hooks.py` green (existing PTC tests untouched).
* `ruff check opencomputer/ plugin_sdk/ tests/` clean.
* Full suite (`pytest tests/`) green — no regression in 9000+ existing tests.
* Manual smoke: `oc hooks doctor` against a real profile, verify reasonable output.

---

## 5. Risk register

| Risk | Mitigation |
|---|---|
| Stdout JSON parsing changes block-decision semantics for an existing user script that happens to emit JSON-shaped stdout | Pre-existing OC scripts emit `{}` (no-op) or empty — both still pass. Only "JSON with recognized block-shape keys" changes behavior. Document in CLAUDE.md migration note. |
| `--execute` invokes a real shell hook with side effects (e.g. git operations) | Same risk as production firing. Document in `oc hooks test --help`: "this fires real handlers; make sure they're idempotent before testing destructive events." |
| `oc hooks doctor` walking `~/.opencomputer/hooks/` may import broken handler.py and crash | Wrap each import in try/except; report import error as ERROR severity. Never raise out of doctor. |
| `max_tool_calls` cap breaks an existing in-the-wild ExecuteCode script that does >50 RPC calls | Default of 50 matches Hermes spec; if a user has a script needing more, `code_execution.max_tool_calls: 100` in config.yaml fixes it. Document in CLAUDE.md alongside the existing `code_execution` slots. |
| Two parallel sessions (config-v2, security-v2 worktrees) might touch shell_handlers.py or cli_hooks.py | Verified: config-v2 commits are in `agent/config.py` + secret-routing; security-v2 commits are in security/sandbox/MCP namespaces. No overlap. |

---

## 6. Out of scope (explicit, with reopen triggers)

* Shell-hook allowlist + consent prompt + `--accept-hooks` flag — see §2.3. Reopen if a user reports a real "didn't realize" incident.
* `hermes_tools` import shim — see §2.3. Reopen if cross-port script-pasting becomes friction.
* Plugin-hook stdout-JSON parity — plugins are Python, they call return-value APIs; stdout protocol is shell-only.
* `code_execution.terminal.background` / `pty` modes — Hermes notes "foreground only" in execute_code. OC matches; no work needed.

---

## 7. Decision log

| Decision | Reasoning |
|---|---|
| Stdout JSON wins over exit code when both present | Mirrors Hermes; mirrors Claude Code (which also lets stdout JSON take precedence over exit codes for advanced fields). Documenting precedence avoids mid-execution ambiguity. |
| `inject_context` field on `HookDecision` rather than new event/decision class | Additive change; existing handlers unaffected; mirrors existing `decision="rewrite"` pattern. |
| `max_tool_calls` enforced in PTC RPC loop, not in `ExecuteCode` wrapper | Cap is per-script tool-call accounting; lives where the counter is. Wrapper passes the limit through. |
| One PR for all 5 items | Each item is small (30-120 LOC); splitting into 5 PRs is overhead. Tests are independent per commit so review-by-commit works. |
| Skip allowlist | Existing OC design treats config.yaml-edit as consent. Adding allowlist duplicates trust boundary; ~200 LOC for marginal value. |
| Doctor walks `~/.opencomputer/hooks/` even when there are 0 hooks | Surface "no gateway hooks installed" as INFO, not silence — helps confirm the directory was scanned. |

---

## 8. Spec self-review

* **Placeholder scan:** None. All gap rows have effort estimates; all decisions logged. ✓
* **Internal consistency:** Numbering G1-G5 used consistently; commit table matches gap table; risk register references the right gaps. ✓
* **Scope check:** Five additive items, one PR, ~310 LOC. Comfortably one execution session. ✓
* **Ambiguity:** Precedence rule (stdout JSON vs exit code) made explicit. `{"context":"..."}` ignored on non-PRE_LLM_CALL events made explicit. ✓

---

## 9. Audit lens results (9-lens framework)

1. **Assumption-check** — Verified parallel worktrees don't touch our files (config-v2 = config namespace, security-v2 = security/sandbox/MCP). Verified Hermes Doc-2 was shipped in PR #496 by reading `2026-05-08-kanban-goals-execcode-hooks-parity.md`. Verified `--execute` is unimplemented by reading `cli_hooks.py:148`. ✓
2. **Architecture stress** — Edge cases mapped: stdout+exit-code precedence, malformed JSON fallback, `{"context":"..."}` from non-injectable events. ✓
3. **Alternative dismissal** — Considered exit-code-only purity (rejected, breaks Hermes parity), full allowlist port (rejected, duplicates trust boundary), doc-only (rejected, leaves `--execute` broken). ✓
4. **Requirement gap** — Tests, CLAUDE.md doc surface, parity findings update all in plan. ✓
5. **Composability** — Stdout JSON + exit-code: stdout-first, exit-code fallback. Doctor reads existing engine state. `--execute` reuses production dispatch. All compose. ✓
6. **Scope honesty** — 5×{30,120,60,50,50} ≈ 310 LOC + ~24 tests. One PR feasible. ✓
7. **API surface drift** — All new flags/configs additive. `inject_context` field optional. Existing tests untouched. ✓
8. **Failure mode** — Engine swallows exceptions (existing); doctor never raises; stdout-JSON fallback path covered; `max_tool_calls` returns clean error. ✓
9. **YAGNI sweep** — Cut allowlist (heavy, marginal value), hermes_tools shim (sugar), unused stdout-shape variants. ✓

**Audit verdict:** No blocking findings. Spec ready for plan.
