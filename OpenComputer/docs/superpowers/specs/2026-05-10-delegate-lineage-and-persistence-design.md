# Delegate Lineage + SubagentRegistry Persistence — Design

**Date:** 2026-05-10
**Branch:** `worktree-delegate-lineage-2026-05-10`
**Author:** session-driven (audit-trace from Saksham, executed by Claude)

---

## 1. Problem statement

A code audit of the `delegate` tool path uncovered six concrete defects ranging from
"silent feature breakage" to "process-wide environment leak":

1. `DelegationCompleteEvent` is published with `parent_session_id=""` literally
   hardcoded (`OpenComputer/opencomputer/tools/delegate.py:657`). Memory
   providers (Honcho) cannot reconstruct delegation lineage from bus traffic.
2. `sessions` table has **no** `parent_session_id` column. Child session rows
   are written, but they are orphans at the schema level — `oc resume <child>`
   works only if you happen to know the id.
3. `SubagentRegistry` is RAM-only (`opencomputer/agent/subagent_registry.py`,
   `dict[str, SubagentRecord]` in module memory). `oc agents history` shows
   nothing across process restarts. Cross-process forensics requires hand-grep
   over `tool_usage` and message bodies.
4. Orchestrator-role demotion at depth boundary
   (`delegate.py:333-340`) silently logs a `WARNING` and returns a leaf result.
   No user-facing surface — only `grep` over logs reveals it.
5. `DelegationConfig.api_key` is written into `os.environ` at delegation start
   (`delegate.py:546-551`) and never cleaned up. The process inherits it
   forever; a subsequent delegation with a different config sees the leaked
   value.
6. The full delegation config surface (`tasks=[...]`, `role="orchestrator"`,
   `forked_context=true`, `isolation="worktree"`, `paths=[...]`,
   `agent="..."`, `allowed_tools`, `DelegationConfig.model`,
   `inherit_credential_pool`) has **zero** production traces. The features are
   shipped but never exercised end-to-end.

This spec closes (1)-(5) in one PR. (6) is closed by adding stress and crash
tests that exercise these paths so a future regression surfaces in CI.

## 2. Goals (and non-goals)

**Goals:**

- A child session's `parent_session_id` is recorded at three layers — bus
  event, registry record, and the child's `sessions` row — every time
  `delegate` runs.
- `oc agents history` and `oc agents running` survive process restart; the
  registry is backed by sqlite.
- `oc sessions tree <session_id>` walks the lineage and renders an ASCII tree
  including each child's role / state / agent template / duration.
- Orchestrator demotion is surfaced in the tool result, not just a log line.
- The api_key env-var leak is closed via context-managed save/restore.
- New tests cover real batch (`tasks=[3 small things]`), `isolation='worktree'`
  smoke, kill-mid-run, and parent-crash cleanup.

**Non-goals (deferred):**

- A periodic heartbeat that detects orphaned `running` records from crashed
  parents in real time. The PID-liveness check at read time is the cheap
  half — full heartbeat needs a background thread and is YAGNI for now.
- Replacing the env-var hack at `delegate.py:546-551` with first-class
  per-provider credential injection. The deeper refactor is provider-plugin
  level and out of scope. The narrow fix (save/restore) is the principled
  bound on blast radius.
- Token-level streaming subscription to a live subagent (no
  `oc agents tail <id>`). Not in audit, easy to add later if asked.

## 3. Audit trace → component map

| Audit defect | Component touched |
| --- | --- |
| (1) empty `parent_session_id` on event | `delegate.py:654-660` publish site |
| (2) sessions table missing column | `state.py` schema + migration v15→v16 |
| (3) RAM-only registry | `subagent_registry.py` + new `subagent_store.py` |
| (4) silent demotion | `delegate.py:333-340` + tool-result content prefix |
| (5) api_key env-var leak | `delegate.py:546-551` save/restore context |
| (6) untested config surface | new tests under `tests/agent/` |

## 4. Design

### 4.1 Schema migration (v15 → v16)

Add to `sessions`:

```sql
ALTER TABLE sessions ADD COLUMN parent_session_id TEXT;
CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
```

New table `subagents` (sqlite-backed mirror of `SubagentRegistry`):

```sql
CREATE TABLE subagents (
    agent_id          TEXT PRIMARY KEY,
    parent_session_id TEXT,
    child_session_id  TEXT,
    parent_agent_id   TEXT,
    goal              TEXT NOT NULL,
    started_at        REAL NOT NULL,
    ended_at          REAL,
    state             TEXT NOT NULL DEFAULT 'running',
    error             TEXT,
    role              TEXT NOT NULL DEFAULT 'leaf',
    agent_template    TEXT,
    isolation_mode    TEXT NOT NULL DEFAULT 'none',
    depth             INTEGER NOT NULL DEFAULT 0,
    host_pid          INTEGER NOT NULL,
    host_started_at   REAL NOT NULL
);
CREATE INDEX idx_subagents_parent_session ON subagents(parent_session_id);
CREATE INDEX idx_subagents_child_session  ON subagents(child_session_id);
CREATE INDEX idx_subagents_state          ON subagents(state);
```

`host_pid` + `host_started_at` are the orphan-detection tuple: a `running`
record whose pid is no longer alive (or whose pid was reused — start-time
mismatch) is reported as `orphaned` at read time.

### 4.2 Plumbing parent_session_id

The publish site at `delegate.py:657` already has access to the parent loop
via `getattr(self._factory, "__self__", None)` (used four times elsewhere in
the file — line 297, 498, 528, 793). The fix is mechanical: read
`parent_loop._current_session_id` and pass it through.

Three wires:

1. `SubagentRegistry.register(parent_session_id=..., role=..., agent_template=..., isolation_mode=..., depth=...)`
   — augmented register signature (default `""` for back-compat).
2. `DelegationCompleteEvent(parent_session_id=...)` — populate the existing
   field instead of literal `""`.
3. `subagent_loop.run_conversation(runtime=child_runtime)` — `child_runtime.custom`
   gets a new key `parent_session_id`. `AgentLoop.run_conversation` reads it
   and passes to `db.create_session(parent_session_id=...)` / `db.ensure_session(parent_session_id=...)`.

### 4.3 SubagentStore — sqlite IO facade

A thin wrapper over the same `sessions.db` file used by `SessionDB`. Owns its
own short-lived connections (open + write + close) so it never holds a long
transaction on the hot path. WAL mode allows concurrent readers/writers.

```python
class SubagentStore:
    def upsert(self, *, agent_id, parent_session_id, child_session_id, ...) -> None: ...
    def update(self, agent_id: str, **fields) -> None: ...
    def history(self, *, limit: int = 50) -> list[StoredSubagent]: ...
    def list_running(self) -> list[StoredSubagent]: ...
    def find_by_parent(self, parent_session_id: str) -> list[StoredSubagent]: ...
```

`SubagentRegistry` integrates via `attach_store(store: SubagentStore)`. Hooks:

- `register()` → in-memory dict + `store.upsert(...)`.
- `update()` → in-memory mutation + `store.update(agent_id, **fields)`.
- `kill()` → in-memory transition + `store.update(agent_id, state="killed", ended_at=...)`.
- `history()` → merge: in-memory ended records + store records (deduped on `agent_id`).

Live-state fields (`cancel_event`, `event_loop`) stay RAM-only; they cannot be
serialized.

`AgentLoop.__init__` calls `SubagentRegistry.instance().attach_store(SubagentStore(self.db.db_path))`
after `SessionDB` is constructed. Tests' autouse fixture detaches the store so
the in-memory dict alone backs registry tests.

### 4.4 `oc sessions tree <session_id>` CLI

New `sessions tree` command. Algorithm:

1. Look up the requested session row. If it doesn't exist, exit 1 with a clean
   error.
2. Walk up first: while `parent_session_id IS NOT NULL`, follow it (max 10
   ancestors to bound the climb).
3. From the discovered root, recursively render children via
   `SELECT id FROM sessions WHERE parent_session_id = ?`. Cap depth at 10.
4. For each child, JOIN with `subagents.child_session_id` so the row carries
   `state`, `role`, `agent_template`, and elapsed time.

Rendered with `rich.tree.Tree` for ASCII + colour. Each node:

```
abc1234 [completed] doc-writer leaf 12.4s — "Summarize the audit findings"
```

### 4.5 Orchestrator demotion surfacing

At `delegate.py:333-340`, when role is demoted from orchestrator → leaf:

- Keep the existing WARNING log.
- Set a flag `_demoted_orchestrator: bool = True`.
- At the result-construction site, prepend a single line to `result.final_message.content`:

      Note: role=orchestrator was demoted to leaf — child would have been at
      max_depth=N with no room to delegate. Re-issue with role=leaf to silence.

  followed by a blank line, then the unchanged content. Prefix is detectable and
  caller-stable.

### 4.6 api_key env-var save/restore

Wrap the env-var assignment block in a try/finally that captures pre-existing
values before mutation and restores them after the child run completes (or
fails). The two keys we touch — `OPENCOMPUTER_DELEGATION_BASE_URL` and
`OPENCOMPUTER_DELEGATION_API_KEY` — have a single read site (the provider
plugin's startup), so a finally-block restore in the same delegate.execute()
frame is sufficient. Concurrent siblings are not affected because each
`DelegateTool.execute` has its own frame and the env-var window is per-call.

### 4.7 Test plan

| Test | What it proves |
| --- | --- |
| `test_migration_v15_to_v16_adds_columns_and_table` | Migration is idempotent; pre-existing v15 rows survive. |
| `test_subagent_registers_with_parent_session_id` | Registry record carries the field. |
| `test_delegation_event_carries_parent_session_id` | Bus event no longer empty. |
| `test_child_session_row_has_parent_session_id` | Schema-level link is written. |
| `test_subagent_store_persists_across_registry_reset` | Cross-process visibility. |
| `test_subagent_store_marks_orphan_when_pid_dead` | Crash detection at read time. |
| `test_oc_sessions_tree_renders_three_generations` | CLI happy path. |
| `test_oc_sessions_tree_unknown_session_id_errors_clean` | CLI error path. |
| `test_orchestrator_demotion_surfaces_in_result_content` | Bomb (4) closed. |
| `test_api_key_env_var_does_not_leak_after_delegation` | Bomb (5) closed. |
| `test_real_batch_three_tasks_all_persist_with_parent_session_id` | (6.a) batch path. |
| `test_isolation_worktree_smoke` (mark `slow`) | (6.b) isolation works. |
| `test_kill_mid_run_marks_record_killed` | (6.c) kill path. |
| `test_parent_baseexception_marks_child_failed` | (6.d) parent crash. |

## 5. Trade-offs

**SubagentStore writes on every register/update/kill.** Cost: one sqlite
WAL append per call. Benefit: cross-process history. The hot path is the
parent loop tool-call; an extra sqlite write is dwarfed by the LLM round
trip. Acceptable.

**Orchestrator demotion prefix is tool_result content.** This means
caller-side parsers see the line. We pick this because the alternative
(adding a structured `delegation_warnings` field to `ToolResult`) is a
public-API change to `plugin_sdk.core.ToolResult`, which is much higher
blast radius. Prefix is detectable, additive, and removable later.

**api_key save/restore vs. dataclass-only wiring.** The "right" fix is
provider-plugin level (pass credentials via `DelegationConfig` directly to
the provider instance, never touch `os.environ`). That's a larger
refactor — out of scope. Save/restore bounds the blast radius without
changing the provider contract.

## 6. Deferred items — honest

The audit listed four "fuzz" items: kill -9 mid-delegation, parent
crash, child OOM, and lock-timeout under concurrent siblings. Of
those, **lock-timeout** and **parent crash** ARE covered by tests in
this PR (see `test_delegate_lineage_e2e_gaps.py`); the other two
deserve an honest writeup of why they're out of scope:

| Item | Why deferred — honestly |
| --- | --- |
| Signal-level kill -9 | Cannot SIGKILL the test process from inside pytest; the receiving end of SIGKILL can't catch anything to record. The cross-process orphan-detection path (host_pid + host_started_at, dead-pid detection at read time) is the structural cover; `test_orphan_detection_across_processes` exercises it end-to-end. |
| Process OOM | Same shape as kill -9 — OS terminates the child. Same orphan-detection cover applies. Triggering real OOM in pytest would need a separate process and explicit memory pressure, both fragile. The path is sound; the test would be flaky for limited extra coverage. |
| Periodic heartbeat for orphan detection | YAGNI. Pid-liveness at read time is enough until users actually complain about stale `running` records. Adding a background heartbeat thread costs complexity; right now the failure mode is observable but not painful. |
| Per-provider first-class credential injection (replacing the env-var hack) | Provider-plugin scope. The env-var save/restore in this PR caps the blast radius (no leaks across delegations or processes). The deeper refactor — pass credentials via dataclass to the provider plugin instance, never touch `os.environ` — is a bigger change and not in the audit. |
| `oc agents tail <id>` live token stream | Not in audit. Ask first. |
| `tasks=[...]` parallel batch return-shape canonicalisation | Existing per-task error handling is correct (`return_exceptions=True` + per-task error formatting). A canonical join shape (e.g. structured failure objects) can wait for a real consumer. |

**What is NOT deferred and shipped in this PR**:

| Audit item | Test that exercises it |
| --- | --- |
| Real `isolation='worktree'` smoke | `test_isolation_worktree_creates_distinct_cwd_for_child` — tmp git repo, real `git worktree add`, child receives distinct cwd. |
| Real `isolation='copy'` smoke | `test_isolation_copy_creates_separate_cwd`. |
| `isolation='worktree'` against non-git cwd | `test_isolation_worktree_on_non_git_cwd_returns_clean_error` — confirms `WorktreeNotAvailable` taxonomy is correct. |
| Concurrent siblings with overlapping paths serialize | `test_concurrent_siblings_with_overlapping_paths_serialize`. |
| Lock-timeout under concurrent siblings | `test_concurrent_siblings_overlapping_paths_timeout_cleanly` — small-timeout coordinator, timeout fires cleanly, lock releasable after. |
| Non-overlapping siblings run in parallel | `test_concurrent_non_overlapping_siblings_run_in_parallel` — sanity inverse. |
| `role='orchestrator'` (when honored, not demoted) | `test_role_orchestrator_when_honored_persists_in_registry` — registry record carries `role='orchestrator'`. |
| `forked_context=True` | `test_forked_context_true_passes_parent_messages_to_child`. |
| Real `AgentLoop` end-to-end persists lineage to sqlite | `test_agent_loop_end_to_end_persists_lineage_to_sqlite` — real loop, stub provider, real db, real lineage column. |
| `AgentLoop.__init__` auto-attaches the store | `test_agent_loop_constructor_attaches_subagent_store_to_singleton` — production zero-opt-in path. |
| Cross-process orphan detection | `test_orphan_detection_across_processes` — process A's dead-pid record is visible to process B as `orphaned`. |

## 7. Migration risk + rollout

The migration is additive (one column + one table). Existing v15 DBs at
runtime will see SCHEMA_VERSION mismatch on startup, run the v15→v16
migration once, and proceed. Rollback path: drop the new table + ignore the
new column (existing readers don't reference it).

CI is the gate: full pytest + ruff before merge. No GH Actions billing
admin-merge shortcuts (per memory rule).
