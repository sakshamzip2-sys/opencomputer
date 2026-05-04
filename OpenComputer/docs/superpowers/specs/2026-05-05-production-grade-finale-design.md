# Production-Grade Finale — Design + Plan + Audit

**Date:** 2026-05-05
**Status:** Brainstorm + plan + audit. Execute now.
**Goal:** Close every honest deferral from prior PRs with full
end-to-end production-grade implementations, not MVPs.

---

## Audit of what's actually deferred

After the prior session's wrap-up, three honest scope notes were
documented but the features themselves were either MVP-scope or
intentionally narrow:

1. **Cross-board cycle detection** (PR #456 deferral) — same-board
   cycles ARE rejected, but cross-board cycles silently land.
   Production-grade requires walking through linked boards' DBs to
   detect graph cycles globally.

2. **Multi-host write coordination** (PR #457 declared "out of scope")
   — only read proxy ships. Full multi-host writes need distributed
   claim, lease-with-server-time TTL, remote spawn, heartbeat
   callbacks, terminal-event webhooks, and HMAC mutual auth. This is
   the biggest gap.

3. **Org-chart views** — Hermes calls these "user-space responsibility";
   we have the data (assignment rules + active assignees) but no
   rendering. Production-grade = a CLI command that draws the tree.

This session ships all three end-to-end.

---

## Brainstorm

### PR-1 — Cross-board cycle detection

**Algorithm:** walk descendants of `child_id` across boards. The walk
expands into:
- Same-board children: existing local task_links query
- Cross-board children: open the target board's DB, query its
  task_links table for outgoing edges from the visited node

Cap recursion at `MAX_HOPS = 64` to prevent pathological data from
hanging the linker. Use a per-walk cache of `(board_slug, task_id)`
to skip re-visited nodes. Treat unreachable boards as leaves (don't
fail the link — same fail-closed reasoning as recompute_ready).

### PR-2 — Multi-host write coordination

**This is the substantial one.** Full design:

#### Data model additions

```sql
-- Hosts that THIS instance trusts to receive write callbacks from.
-- HMAC-signed bearer tokens; one row per peer.
CREATE TABLE kanban_remote_hosts (
    slug          TEXT PRIMARY KEY,    -- short id used in assignee strings
    url           TEXT NOT NULL,       -- e.g. https://host-b.local:9119
    hmac_secret   TEXT NOT NULL,       -- shared secret for both directions
    added_at      INTEGER NOT NULL,
    last_seen_at  INTEGER              -- updated on successful health check
);

-- Pending tasks that THIS host has delegated to a remote host.
-- Remote sends terminal callback → row gets reconciled.
CREATE TABLE kanban_remote_claims (
    local_task_id  TEXT NOT NULL,      -- task on this host
    remote_slug    TEXT NOT NULL,      -- which remote host
    remote_task_id TEXT NOT NULL,      -- mirrored task id on remote
    leased_at      INTEGER NOT NULL,   -- server-side timestamp
    lease_until    INTEGER NOT NULL,   -- server-side TTL
    status         TEXT NOT NULL,      -- pending | running | done | failed
    last_heartbeat INTEGER,
    PRIMARY KEY (local_task_id, remote_slug)
);
```

#### Wire protocol

`POST /api/plugins/kanban/proxy/spawn`:
```json
{
  "schema_version": 2,
  "task": {
    "id": "...", "title": "...", "body": "...",
    "assignee": "<profile-on-remote>",
    "priority": 0, "tenant": null,
    "workspace_kind": "scratch",
    "workspace_payload": null    // optional dir tarball if dir-kind
  },
  "callback_url": "https://this-host:9119/api/plugins/kanban/proxy/callback",
  "callback_token": "<HMAC>"
}
```
Response: `{remote_task_id, lease_until}`.

`POST /api/plugins/kanban/proxy/heartbeat`:
```json
{"local_task_id": "...", "remote_task_id": "...", "now": <server_ts>}
```
Response: `{lease_until: <new>}`. Server enforces server-time TTL —
clients can't fake a heartbeat from the future.

`POST /api/plugins/kanban/proxy/callback`:
```json
{
  "schema_version": 2,
  "remote_task_id": "...",
  "outcome": "done" | "blocked" | "failed",
  "summary": "...", "metadata": {...}, "error": "..."
}
```
HMAC-signed with the host's `hmac_secret`. Verifying host A reconciles
its `kanban_remote_claims` row + transitions the local task.

#### Dispatcher integration

When a task's `assignee` matches `<remote_slug>/<profile>`, the
dispatcher routes to the remote-spawn path instead of `_default_spawn`.
The remote_slug must be in `kanban_remote_hosts`; otherwise the task
gets `auto_blocked` with a clear reason.

#### Auth

HMAC-SHA256 signature over `(timestamp, method, path, body_sha256)`
in the `X-OC-Signature` header. Replay protection: reject signatures
older than 5 minutes. Mutual: each host stores the peer's secret and
signs outgoing requests with it.

#### Workspace handling

Initial scope: `scratch` and `worktree` kinds work without remote
sharing (each host has its own workspace dir). For `dir:<path>` we
document that the path must exist on the remote (shared FS or
out-of-band sync). A future PR could add HTTP file-payload sync;
for now we send the workspace path as a string and the remote resolves
it locally.

This is the same compromise hermes makes implicitly (single-host).
We expand the surface so remote spawn + lease + callback work, but
workspace data sharing is its own follow-up tier.

### PR-3 — Org-chart CLI

`oc kanban orgchart [--json] [--depth N]`:

Reads `kanban_assignment_rules` + active task assignees, builds a
tree:

```
└── Auto-routing rules
    ├── [priority 100] title_regex: ^deploy:    → deploy-bot
    ├── [priority 50]  tenant: ops               → ops-team
    └── [priority 0]   default: *                → catch-all-bot

└── Active workers (from running tasks)
    ├── deploy-bot      (3 running, 2 done last 24h)
    ├── ops-team        (1 running, 7 done last 24h)
    └── catch-all-bot   (0 running, 12 done last 24h)
```

`--json` emits a machine-readable structure for dashboards.

---

## Plan

### PR-1 — Cross-board cycle detection (~250 LOC)

**Branch:** `feat/wave6-cross-board-cycle-detect`

1. New `_would_cycle_global(conn, parent_id, child_id, parent_board=None, child_board=None) -> bool`:
   - Walk descendants of `(child_board, child_id)`
   - Cross-board children resolved via short-lived sqlite3 connection
   - Cap MAX_HOPS = 64
   - Cache visited `(slug, task_id)` tuples
2. `link_tasks` calls the global walker for cross-board edges (same-
   board still uses the existing optimized walker)
3. Tests: same-board cycle rejected (regression), cross-board direct
   cycle rejected (A→B→A), cross-board indirect cycle rejected
   (A→B→C→A), depth cap, missing-board treated as leaf

### PR-2 — Multi-host write coordination (~1300 LOC)

**Branch:** `feat/wave6-kanban-multi-host-writes`

1. Schema migration: 2 new tables (kanban_remote_hosts,
   kanban_remote_claims) with indices
2. New `opencomputer/kanban/remote_hosts.py`:
   - `add_remote_host`, `list_remote_hosts`, `remove_remote_host`,
     `find_host_by_slug`
   - `sign_request(method, path, body, secret)` → HMAC header value
   - `verify_request(headers, method, path, body, secret)` → bool +
     replay-window check
3. New `opencomputer/kanban/remote_dispatch.py`:
   - `delegate_task_to_remote(conn, task, host) -> str` (returns
     remote_task_id) — POSTs to `/proxy/spawn`, records the claim
   - `heartbeat_remote_claim(conn, local_task_id, remote_slug)` —
     periodic refresh; called from dispatcher
   - `reconcile_callback(conn, payload)` — applies a terminal
     callback to the local task
4. Dashboard plugin extensions:
   - `POST /api/plugins/kanban/proxy/spawn` — remote calls this on us
   - `POST /api/plugins/kanban/proxy/heartbeat`
   - `POST /api/plugins/kanban/proxy/callback`
   - All require HMAC signature; replay-window 300s
5. CLI: `oc kanban remote {add,list,rm,test,status}`
6. Dispatcher integration in `_default_spawn`: detect `slug/profile`
   form in `task.assignee`; if `slug` is a registered remote host,
   delegate instead of forking locally
7. Heartbeat tick on the kanban dispatcher loop refreshes leases
8. Tests: 20+ across schema, helpers, HMAC sign/verify (good +
   tampered + expired), spawn endpoint, heartbeat endpoint, callback
   endpoint, dispatcher routing, full round-trip via TestClient

### PR-3 — orgchart CLI (~300 LOC)

**Branch:** `feat/wave6-kanban-orgchart`

1. `_cmd_orgchart(args)` in `opencomputer/kanban/cli.py`:
   - Aggregates rules + active assignees
   - Renders ASCII tree (uses `└── ├── │`)
   - `--json` returns structured dict
   - `--depth N` caps how deep we recurse into rule chains
2. Tests: 6+ (rules-only, assignees-only, both, JSON output, --depth
   cap, empty-board edge case)

---

## Self-audit (10 lenses)

### A1. HMAC replay protection
A signed request from 6 minutes ago shouldn't work today. **Refinement:**
include `timestamp` in the signed payload; reject if `abs(now - ts) > 300`.

### A2. HMAC clock-skew across hosts
Two hosts with skewed clocks could legitimately disagree on "now".
**Refinement:** the 300s window absorbs up to 2.5 minutes of skew
either direction. Document that skew >150s breaks the protocol —
ops responsibility to NTP-sync hosts.

### A3. HMAC body hash
Can a man-in-the-middle replay a body? **Refinement:** signature
covers `sha256(body)` so swapping the body invalidates the signature.

### A4. Cross-board cycle walker performance
A graph with thousands of cross-board edges could be slow to walk.
**Refinement:** the per-walk cache + `MAX_HOPS=64` cap bounds runtime
at O(64 × per-board lookup). Per-board lookup is O(1) for the cached
set after first visit.

### A5. Lease + dispatcher race
Two dispatchers could both delegate the same task. **Refinement:**
the local `kanban_remote_claims` insert uses INSERT OR ABORT (PRIMARY
KEY collision = task already delegated) inside the same write_txn
that claims the task locally; the dispatcher reads the claim row
back before deciding to spawn.

### A6. Callback authorization
Anyone with the URL could send a fake "done" callback. **Refinement:**
HMAC signature is required AND the `remote_task_id` must match an
active row in `kanban_remote_claims`. Two-factor — neither alone is
sufficient.

### A7. Workspace `dir:<path>` on remote
The path may not exist on the remote. **Refinement:** the spawn
endpoint validates the path exists locally before claiming the task;
if it doesn't, return 422 "workspace path missing on remote" so the
sender can re-route to a different host. Documented limitation:
shared-FS workspaces are the user's responsibility.

### A8. Failed callbacks
Network drop between host B's complete and the callback POST to A.
**Refinement:** host B retains the callback in a persistent queue
(reuse `OutgoingDrainer` — payload is just an HTTP POST). Heartbeat
from A side detects expiry and reclaims if the callback never arrives.

### A9. Org-chart depth cap
A circular ruleset isn't possible (rules don't reference other rules)
but the tree COULD recurse into per-rule task lists. **Refinement:**
`--depth` defaults to 2 (rules → assignees → counts). Tests cover
depth=0 (rules only), depth=1 (rules + assignees), depth=2 (full).

### A10. Migration safety
Schema migration adds tables — fully additive, existing single-host
users see zero change until they `oc kanban remote add`. No data
migration needed.

---

## Final plan summary

| PR | Title | LOC | Tests |
|---|---|---|---|
| 1 | Cross-board cycle detection | ~250 | 6+ |
| 2 | Multi-host write coordination (full distributed) | ~1300 | 20+ |
| 3 | oc kanban orgchart CLI | ~300 | 6+ |

Total ~1850 LOC + 32+ tests across 3 PRs. Execute now.
