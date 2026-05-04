# Hermes Out-of-Scope Items — Finale Design

**Date:** 2026-05-05
**Status:** Brainstorm + plan + audit. Execute now.
**Inputs:** Hermes docs declare these "out of scope"; user wants them shipped.

---

## Goal

Close the three items Hermes itself documents as out-of-scope:

1. Multi-host coordination
2. Cross-board task dependencies
3. Auto-assignment routing / org-chart views

Honest scope discipline applied below — auto-assignment ships
end-to-end; cross-board ships with full schema migration; multi-host
ships a deliberately minimal "remote read" surface with documented
limits because true distributed coordination is a multi-week project,
not a session.

---

## Brainstorm

### Item 1 — Multi-host coordination (the hard one)

Hermes scoped this out for good reason. Sharing a SQLite board across
hosts is broken in subtle ways:

- **NFS lock semantics are unreliable.** SQLite WAL mode requires
  shared mmap; NFS lacks it. Even POSIX flock over NFS varies by
  client/server combo.
- **Claim TTLs across clocks.** Different hosts see different "now"
  values; stale-claim reclamation across hosts is racy.
- **Workspace sharing.** A worker on host A can't access scratch dir
  on host B unless the workspace is on shared storage.
- **Crash detection.** `kill(pid, 0)` only works for local PIDs.

**Realistic minimum that's actually useful:**

A **remote-board read proxy** — one host (the "viewer") can READ
another host's board over an HTTP API for monitoring + reporting.
NOT for writing tasks or claiming. Documented as such.

This unblocks:
- Manager profile on host A monitoring worker board on host B
- Cross-host visibility into kanban state
- Future-proof: the same HTTP surface can grow to support write
  operations once the harder distributed-systems pieces land

**What's deferred (honest):**
- Remote claim / dispatch (would need distributed locks + clock-skew
  handling)
- Cross-host workspace sharing (needs shared FS)
- Full distributed reclamation

The PR doc explicitly calls these out so users don't try to
hot-network a multi-host write workflow and find correctness gaps.

### Item 2 — Cross-board dependencies

Schema today: `task_links(parent_id TEXT, child_id TEXT)`. Both ids
must reference rows in the same SQLite file. Cross-board means a
parent in board A blocks a child in board B.

**Schema migration:** add `parent_board_slug TEXT NULL` and
`child_board_slug TEXT NULL`. NULL = same board (back-compat). When
either is non-NULL, the link spans boards.

**Read path:** `recompute_ready` queries the parent's board file when
`parent_board_slug` is set. SQLite ATTACH DATABASE works for this
when both files are local; for remote boards we fail-closed (don't
promote — hold in `todo` until the parent's board is resolvable).

**Write path:** `oc kanban link <child> --parent <id> --parent-board <slug>`
adds the cross-board edge.

**Worker context:** the `worker_context` JSON returned by `kanban_show`
includes parent summaries; we cross-attach to read them.

**Scope discipline:** cross-board reads work for ALL local boards
(tested). Cross-board reads to a remote board return "unresolved"
(future work alongside multi-host writes).

### Item 3 — Auto-assignment routing

Most tractable item. Tasks today require an explicit `--assignee` —
when omitted the dispatcher skips them. Many users have stable
patterns (e.g. "tasks with 'deploy' in title go to deploy-bot
profile"). A small rules table covers it.

**Schema:** new `kanban_assignment_rules` table:

```sql
CREATE TABLE kanban_assignment_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_kind TEXT NOT NULL,  -- 'title_regex' | 'tenant' | 'default'
    pattern TEXT NOT NULL,        -- regex / tenant name / '*' for default
    assignee TEXT NOT NULL,
    priority INTEGER DEFAULT 0,   -- higher = checked first
    created_at INTEGER NOT NULL
);
```

**Read path:** `dispatch_once`'s ready-task loop, when it sees a task
with `assignee IS NULL`, runs the rules in `priority DESC, id ASC`
order. First match wins. Rule kinds:

- `title_regex`: re.search(pattern, task.title) — pre-compiled and
  cached per dispatch tick
- `tenant`: exact match on task.tenant
- `default`: always matches (catch-all)

**CLI:** `oc kanban rules {add,list,rm,test}`:

- `add --kind title_regex --pattern "^deploy:" --assignee deploy-bot --priority 100`
- `list` → table view
- `rm <id>`
- `test "<task title>" [--tenant X]` → shows which rule would match

---

## Plan (executable)

### PR-A — Auto-assignment routing rules

**Branch:** `feat/wave6-kanban-auto-assignment`
**LOC:** ~350

1. Schema migration in `opencomputer/kanban/db.py`:
   - New `kanban_assignment_rules` table
   - Helpers: `add_assignment_rule()`, `list_assignment_rules()`,
     `delete_assignment_rule()`, `resolve_assignee(conn, task)`
2. `dispatch_once`: when a ready row has no assignee, call
   `resolve_assignee` before the claim attempt; assign + persist.
3. CLI: `oc kanban rules {add,list,rm,test}`
4. Tests: 12+ (schema, helpers, resolution priority, regex match,
   tenant match, default catch-all, dispatch integration, CLI roundtrip)

### PR-B — Cross-board task dependencies

**Branch:** `feat/wave6-kanban-cross-board-links`
**LOC:** ~500

1. Schema migration: `task_links` add `parent_board TEXT NULL` and
   `child_board TEXT NULL` columns. Existing rows get NULL = back-
   compat (same-board link).
2. Helpers: `link_tasks(parent_id, child_id, *, parent_board=None, child_board=None)`
3. `recompute_ready`: when a parent has `parent_board != NULL` AND
   that board's DB file exists locally, ATTACH and query. When it
   doesn't (remote / missing), hold the child in `todo` and log.
4. CLI: `oc kanban link <child> --parent <id> [--parent-board <slug>]`
5. `kanban_show` worker context fetches cross-board parent summaries
   when resolvable.
6. Tests: 10+ (schema migration, same-board still works, cross-board
   local works, cross-board missing-DB holds in todo, CLI link arg)

### PR-C — Multi-host: remote-board read proxy

**Branch:** `feat/wave6-kanban-remote-read`
**LOC:** ~400

1. New `opencomputer/dashboard/plugins/kanban/remote_proxy.py` exposing:
   - `GET /api/plugins/kanban/proxy/board?slug=<slug>` — returns
     full board state JSON (tasks + links + tenant filters)
   - `GET /api/plugins/kanban/proxy/task/<id>?board=<slug>` — single
     task + comments + run history
   - `GET /api/plugins/kanban/proxy/health` — alive + slug-list
   - All token-gated (existing dashboard auth)
2. New `opencomputer/kanban/remote_client.py`:
   - `class RemoteKanbanClient`: HTTP client for the proxy endpoints
   - Drop-in for read-only board operations
3. CLI: `oc kanban remote {add,list,rm,show}` to register host URLs
   and view remote boards
4. **Documented constraints (PR description):**
   - Read-only — no claiming or writing across hosts
   - Workspace data NOT shared
   - Future-work line for multi-host writes
5. Tests: 8+ (proxy routes return 200, remote client parses, missing
   board 404, bad token 401, CLI roundtrip)

---

## Self-audit

### A1. Silent API drift
- `dispatch_once`'s claim loop is the right insertion point for auto-
  assignment — verified by reading lines 700+ in db.py.
- `task_links` schema has no extra columns — adding them via
  ALTER TABLE in a migration is safe.
- `recompute_ready` walks task_links via JOIN — adding the cross-board
  branch needs ATTACH DATABASE which SQLite supports.

### A2. Auto-assignment + idempotency
If a task is created with explicit `--assignee X` AND a rule would
match, the explicit value wins (rules only fire when assignee IS NULL).
Documented + tested. ✅

### A3. Auto-assignment regex DOS
A user could add `pattern = "(a+)+"` (catastrophic backtracking).
**Refinement:** wrap each regex compile in a try/except; bad regexes
get rejected at `add` time. Compile once per dispatch tick, not per
task, and use `re.compile` with no flags by default.

### A4. Cross-board ATTACH leaks
SQLite's `ATTACH DATABASE` lasts for the connection lifetime. If we
ATTACH on every dispatch tick we leak schema-attached connections.
**Refinement:** open a SHORT-LIVED connection for cross-board reads,
ATTACH inside, query, close. Don't ATTACH on the long-lived dispatch
connection.

### A5. Cross-board dependency cycles
A → B → A across boards is detectable at link-add time by walking
parents. **Refinement:** cycle-check before INSERT into task_links.
Reject with a clear error.

### A6. Remote-read proxy: rate limits + auth
The proxy serves over HTTP. **Refinement:** mirror the existing
dashboard token gate (`require_session_token`). Document that the
token MUST NOT be shared across hosts that aren't trusted (the host
holding the token gets full board read access).

### A7. Remote-read proxy: schema drift
Host A on schema v15, host B on schema v14. The proxy returns rows
that include columns the client doesn't know about. **Refinement:**
the JSON wire format wraps rows in a versioned envelope:
`{"schema_version": 1, "rows": [...]}`. Future-work TODO for
backward-incompat schema changes.

### A8. Test infrastructure
All three PRs use the kanban_home fixture pattern from the existing
multi-board tests. CLI tests use the same `_run_cli` helper.

### A9. Dispatch ordering
Auto-assignment must happen INSIDE the BEGIN IMMEDIATE txn that
claims the task — otherwise two workers could be dispatched the same
task (one assignee from rules, one stays unassigned, then both try
to claim). Verified placement in dispatch_once below.

### A10. Multi-host: what we honestly DON'T do
The proxy is read-only by design. The PR description must call this
out so users don't try to wire it as a write surface. We document:
- "Use this for monitoring only."
- "For multi-host writes, run separate boards per host and use
  cross-board deps (PR-B) to express ordering."

---

## Final plan summary

| PR | Title | Branch | LOC | Tests |
|---|---|---|---|---|
| A | Auto-assignment routing rules | feat/wave6-kanban-auto-assignment | ~350 | 12+ |
| B | Cross-board task dependencies | feat/wave6-kanban-cross-board-links | ~500 | 10+ |
| C | Remote-board read proxy | feat/wave6-kanban-remote-read | ~400 | 8+ |

Total ~1250 LOC. Execute now.
