# Multi-host Wiring — Design + Plan + Audit

**Date:** 2026-05-05
**Status:** Brainstorm + plan + audit. Execute now.

---

## What's actually pending

PR #460 (Wave 6.E.13) shipped the multi-host primitives (HMAC, leases,
spawn endpoint, callback endpoint, register CLI), but the verification
in this round caught two genuine holes:

1. **`_default_spawn` doesn't check for remote assignees.** A task with
   `assignee="peer-slug/profile-x"` falls through to the local-fork
   subprocess path, which would `oc -p peer-slug/profile-x chat -q ...`
   and fail. The end-to-end write path never fires.

2. **Dispatcher loop never heartbeats pending remote claims.** PR #460
   exposed `heartbeat_remote_claim` but nothing periodically calls it.
   Leases expire while peer workers are still running → spurious
   reclaim attempts.

3. **Failed callbacks lost.** PR #460 audit lens A8 noted "future retry
   queue is its own follow-up". Peer-side: if the callback POST to
   sender fails, the result data + summary is lost. The peer keeps the
   task in `done` state but the sender's lease eventually expires and
   the local task is orphaned.

These together mean the multi-host write path documented in PR #460 is
NOT actually production-functional today. This round closes all three.

---

## Brainstorm

### Item 1 — Dispatcher delegates remote tasks

`_default_spawn(task, workspace)` is the spawn function `dispatch_once`
calls. Today it builds an argv + subprocess.Popen for `oc chat -q ...`.

Approach: at the top of `_default_spawn`, check
`parse_remote_assignee(task.assignee)`. If it returns `(slug, profile)`:

1. Open a kanban DB connection
2. `find_remote_host(conn, slug)` → host or None
3. If host found: `delegate_task_to_remote(...)` and return None (no
   local PID — the work is on the peer)
4. If host NOT found: log + raise (the assignee references an unknown
   peer; let the spawn-failure-counter eventually auto-block)

Returning None for the PID is fine — `_set_worker_pid(... pid=None)`
is a no-op + the task stays in 'running' state with the kanban_remote_claims
row tracking liveness instead.

But: `dispatch_once` records the spawn into `result.spawned` and bumps
the counter. We DO want that — the task IS spawned, just remote.

### Item 2 — Heartbeat tick

Add a phase to the kanban dispatcher loop that walks
`list_pending_remote_claims(conn)` and heartbeats any lease that
expires within `HEARTBEAT_LEAD_SECONDS` (60s default).

`heartbeat_remote_claim` already handles the round-trip + lease update.
On network failure: log + retry next tick. On 401/410: mark the claim
as `failed` so the dispatcher's reclaim path picks it up.

### Item 3 — Callback retry queue (peer side)

When peer's worker completes and posts the callback to sender, the POST
might fail (network blip, sender restarting). Today: lost forever.

Production-grade design:

- New table: `kanban_pending_callbacks`
  - `id INTEGER PK AUTOINCREMENT`
  - `remote_slug TEXT NOT NULL` (which peer registered the task)
  - `payload_json TEXT NOT NULL` (the callback body)
  - `attempt_count INTEGER NOT NULL DEFAULT 0`
  - `next_attempt_at INTEGER NOT NULL` (unix epoch)
  - `last_error TEXT`
  - `created_at INTEGER NOT NULL`
- Peer worker calling complete/block writes the callback to the queue
  instead of POSTing directly
- A new "callback drainer" task in the kanban dispatcher loop polls
  the queue every tick, sends signed POSTs, removes on 2xx, bumps
  attempt_count + exponential backoff on failure
- After 10 attempts (cap): mark dead-letter, log, leave row for
  operator review
- Idempotency: callbacks include `remote_task_id` which the SENDER's
  reconcile_callback already verifies against an active claim. A
  duplicate callback (because attempt 3 actually succeeded but our
  sender response was dropped) sees `find_claim_by_remote_id` →
  status=done → no-op. Safe.

---

## Plan

### PR-A: dispatcher delegation + heartbeat tick (~400 LOC)

**Branch:** `feat/wave6-multihost-wiring`

1. Modify `_default_spawn` in `kanban/db.py`:
   - Top of function: detect remote assignee via `parse_remote_assignee`
   - Lookup host via `find_remote_host`
   - On hit: build a Task object reflecting current state, call
     `delegate_task_to_remote(...)`, return None
   - On unknown slug: raise `ValueError(f"unknown peer slug: {slug}")` —
     the existing spawn-failure path auto-blocks after 5 retries
2. Add `_tick_heartbeats` to `gateway/kanban_dispatcher.py`:
   - After `dispatch_once` each tick, scan
     `list_pending_remote_claims(conn)` for claims where
     `lease_until - now < HEARTBEAT_LEAD_SECONDS`
   - For each, lookup host, call `heartbeat_remote_claim`
   - On `RemoteDispatchError`: log + skip (try again next tick)
3. Tests (10+):
   - `_default_spawn` with `peer/profile` assignee delegates
   - `_default_spawn` with unknown peer slug raises
   - `_default_spawn` with no slash falls through to local subprocess
   - Heartbeat tick hits hosts with claims expiring soon
   - Heartbeat skips claims with comfy lease

### PR-B: callback retry queue (~500 LOC)

**Branch:** `feat/wave6-callback-retry-queue`

1. Schema migration: `kanban_pending_callbacks` table (additive)
2. New `opencomputer/kanban/callback_queue.py`:
   - `enqueue_callback(conn, remote_slug, payload)` → row id
   - `next_due(conn, now) -> list[(id, slug, payload)]`
   - `mark_attempted(conn, row_id, *, error=None, max_attempts=10)`
     — bumps count + exponential backoff OR marks dead-letter
   - `mark_delivered(conn, row_id)` — deletes row
3. Update kanban_complete / kanban_block / kanban_failed handlers:
   - When the local task is a peer-claimed task (we look it up via
     `kanban_remote_claims.remote_task_id == task.id`), enqueue
     callback instead of returning normally. Otherwise local-only path.
4. New "callback drainer" tick in `gateway/kanban_dispatcher.py`:
   - Each tick: walk `next_due(conn)`, sign, POST to peer's
     `/proxy/callback`, mark delivered or attempted
5. Tests (12+):
   - enqueue → next_due returns it
   - exponential backoff (attempt 1 → +30s, attempt 2 → +60s, ...)
   - max_attempts cap dead-letters the row
   - drainer happy-path: enqueue → tick → delivered
   - drainer 5xx → row stays + backoff
   - dead-letter: 10 failed attempts, status=dead

---

## Self-audit (10 lenses)

### A1. Delegate from inside _default_spawn — sync vs async
`_default_spawn` is synchronous (called from `dispatch_once`). But
`delegate_task_to_remote` uses sync httpx — that's fine. ✅

### A2. Connection contention
`_default_spawn` doesn't take a conn arg. It opens its own short-lived
sqlite3 connection for the `find_remote_host` lookup. Use `kdb.connect()`
not `sqlite3.connect(str(path))` so init_db runs.

### A3. dispatch_once recording
`result.spawned.append((task.id, assignee, workspace))` happens AFTER
spawn_fn returns. For remote delegation, workspace is empty string +
PID is None — both fine for the spawned tuple shape.

### A4. Heartbeat thundering herd
If a peer is down and we have 10 claims pointing at it, every heartbeat
fails and we get 10 errors per tick. **Refinement:** group claims by
slug; for each slug, do one heartbeat per tick at most? Actually no —
each claim has its own remote_task_id. 10 separate POSTs is correct.
Just suppress the per-claim error log to debug after the first.

### A5. Heartbeat 401 vs 5xx
- 401 means our HMAC secret is wrong → never recover; mark claim failed
- 410 means the remote claims the task is gone → mark claim dead
- 5xx / network error → transient, retry next tick

`heartbeat_remote_claim` raises `RemoteDispatchError` with the message
in either case. **Refinement:** parse the status code into the error
type so the dispatcher can decide. For this PR keep it simple: log +
skip on any error; the lease eventually expires and the existing
reclaim path handles it.

### A6. Callback retry — backoff schedule
- Attempt 1 fails → next_attempt_at = now + 30s
- Attempt 2 fails → +60s
- Attempt 3 → +120s
- Attempt 4 → +300s
- Attempts 5-9 → +600s each
- After 10 → dead-letter (status = 'dead', kept for operator review)

Total time before dead-letter: ~80 minutes. Long enough to survive
overnight peer downtime.

### A7. Callback idempotency
Sender's `reconcile_callback` rejects callbacks for claims that aren't
active (status != pending). Duplicate delivery from a successful retry
sees the row already done → no-op. ✅

### A8. Schema migrations are additive
`kanban_pending_callbacks` is a new table. Existing rows untouched.
Single-host installs that never register peers see zero change.

### A9. Drainer interval
Dispatcher loop ticks every 5s by default. Callback drainer runs in
the same tick. 5s is fine — peers waiting for terminal events won't
notice the lag.

### A10. Test fixture isolation
All 22+ tests use the existing `kanban_home` tmp_path fixture.
HTTP mocking via `unittest.mock.patch('httpx.post')`.

---

## Final plan summary

| PR | Title | LOC | Tests |
|---|---|---|---|
| A | dispatcher delegation + heartbeat tick | ~400 | 10+ |
| B | callback retry queue | ~500 | 12+ |

Total ~900 LOC + 22+ tests. Execute now.
