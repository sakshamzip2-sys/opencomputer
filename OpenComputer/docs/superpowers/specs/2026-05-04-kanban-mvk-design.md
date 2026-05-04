# Minimal-Viable Kanban — Design Spec

**Date:** 2026-05-04
**Status:** Approved (auto-mode shipping)
**Source:** Hermes-port `c86842546` (full Kanban) — explicitly scoped down.
**Karpathy "Simplicity First" cut:** Hermes shipped 22 commits in one PR. We ship the persistence layer + 3 core tools + CLI; dispatcher and dashboard plugin are 6.B-β.

## 1. Problem

OC has no durable task-board primitive. Hermes ships a Kanban with persistent tasks, multi-profile collaboration, and a dispatcher that auto-spawns sibling agents to work tasks. Porting all of it in one session is irresponsible (~2500 LOC). This spec ships the **storage + tool surface** so the agent can interact with a Kanban from inside a worker session — without yet adding the dispatcher or dashboard UI.

## 2. Scope

### In (this PR)
- `kanban_tasks` table: schema migration v12→v13 (additive, idempotent).
- 3 tools (BaseTool subclasses, gated on `OC_KANBAN_TASK` env so default sessions don't see them):
  - `KanbanCreate` — create a task with title/description/assignee
  - `KanbanShow` — list tasks (filter by status/assignee) or show one by id
  - `KanbanComplete` — mark a task done, optionally with a completion note
- 5 CLI subcommands: `oc kanban {init,list,create,show,complete}`
- Tests: schema migration + 3 tools + CLI smoke

### Out (deferred to 6.B-β)
- `KanbanBlock`, `KanbanHeartbeat`, `KanbanComment`, `KanbanLink` tools
- Kanban dispatcher (spawns `oc -p worker chat -q "work task <id>"` subprocesses with `OC_KANBAN_TASK` env)
- Linear-style dashboard UI plugin (drag-drop, WebSocket live refresh)
- Multi-profile lanes, task_runs history, idempotency keys, circuit breaker

## 3. Architecture

```
opencomputer/kanban/
  __init__.py        — re-exports for ergonomic ``from opencomputer.kanban import …``
  db.py              — CRUD on the existing SessionDB connection. Pure-function API.
  tools.py           — KanbanCreate / KanbanShow / KanbanComplete subclassing BaseTool
  cli.py             — Typer subcommand group registered as ``oc kanban``

opencomputer/agent/state.py
  + _migrate_v12_to_v13  — additive ALTER + new table
  + DDL ``kanban_tasks`` block for fresh DBs
```

### Schema

```sql
CREATE TABLE kanban_tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'todo',
        -- Allowed: triage, todo, ready, running, blocked, done
    assignee        TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    blocked_reason  TEXT,
    completion_note TEXT
);
CREATE INDEX idx_kanban_tasks_status ON kanban_tasks(status);
CREATE INDEX idx_kanban_tasks_assignee ON kanban_tasks(assignee);
```

### Tool gating

Each kanban tool's `is_available()` returns True only when `OC_KANBAN_TASK` is set in the env. The dispatcher (6.B-β) will set this when spawning a worker. Default chat sessions never see these tools.

## 4. Testing

- Unit: schema migration v12→v13 idempotent + adds table.
- Unit: db.create / db.list / db.complete round-trip.
- Unit: each tool's `execute()` happy path + invalid status guard + missing-task-id guard.
- CLI smoke: `oc kanban init` creates table; `create/list/show/complete` round-trip via Typer's CliRunner.

## 5. Risks

- **Status enum drift:** stored as TEXT, not enum. New states need `STATUS_VALUES` constant update.
- **Concurrent writes:** all writes go through `SessionDB._txn()` which has flock + retry-jitter.
- **Tool collision:** prefixed `Kanban*` to namespace cleanly.
- **Migration on populated DB:** additive `CREATE TABLE IF NOT EXISTS` — safe.

## 6. Success criteria

- All new tests green.
- Schema migration test still green (with v13 assertion update).
- Existing 9000+ tests still pass.
- A user can run `oc kanban create "fix the build" --assignee=me` followed by `oc kanban list` and see the task.
