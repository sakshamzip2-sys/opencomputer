# Wave 6 Final Limitations — Design + Plan + Audit

**Date:** 2026-05-05
**Status:** Brainstorm + plan + audit. Execute now.
**Goal:** Close the two documented limitations from PRs #460 and #458.

---

## Brainstorm

### A. Cross-host `dir:<path>` workspace sync

**Today:** PR #460 ships `scratch` and `worktree` cross-host (each peer
allocates its own dir); `dir:<path>` requires the path to exist on the
peer, otherwise the spawn endpoint returns 422. Operator must arrange
shared FS out-of-band.

**Production-grade alternatives:**

1. **HTTP tarball payload** — sender builds a tarball of the workspace,
   includes it as base64 in the spawn POST. Peer extracts to its own
   tmp dir. On callback, peer ships modified contents back as a return
   payload; sender reconciles by extracting into the original
   `dir:<path>`. Self-contained, no operator FS setup.

2. **SSH/rsync delegation** — sender invokes rsync against a configured
   shared host. Requires SSH keys + a shared host. Operator-heavy.

3. **Object-store integration** — push to S3/GCS, peer pulls down.
   Requires cloud creds. Heavy.

Pick **A.1**. Self-contained, no extra deps (Python ships `tarfile`),
operator-zero. Cap total payload at 50 MB to prevent abuse +
documentation that scratch is faster for big work.

**Wire surface additions:**
- Spawn request body gains optional `workspace_payload_b64` (base64 of
  a gzipped tarball). When present, peer:
  - Decodes + extracts to a fresh tmp dir
  - Sets the new local task's `workspace_path` to that dir
  - The task's `workspace_kind` stays `dir`
- Callback body gains optional `workspace_payload_b64` for the return
  trip. When present, sender:
  - Decodes + extracts INTO the original `dir:<path>` location
  - Reconciles + transitions the local task

**Safety:**
- Total payload size cap: 50 MB on both sides
- Tarfile extraction uses `extractall(filter='data')` (Python 3.12+)
  to reject absolute paths + symlink escapes (CVE-2007-4559)
- Path validation: caller must pass an absolute, normalized path
- Per-host opt-in: `kanban_remote_hosts` gets a `workspace_sync_enabled`
  column (default 0); only when both sides have it set does payload
  sync activate

### B. Legacy default board cycle detection

**Today:** the cycle walker scans `list_boards()` (named boards only).
The legacy unnamed default board is invisible to the walker — links
between named boards and the default can land in cycles undetected.

**Fix:** treat the default as a real first-class participant in the
walker.

- Reserve a sentinel slug `_default_` (with leading + trailing
  underscores; matches an updated regex but doesn't collide with user
  slugs that disallow leading underscore)
- `board_db_path("_default_")` returns `kanban_home() / "kanban.db"`
  (legacy path)
- `validate_slug` allows the sentinel
- `_would_cycle_global` walker reads the legacy default's task_links
  alongside named boards
- Users can explicitly express edges to/from default: `parent_board="_default_"`
- `set_active_board("_default_")` clears the active-board file

**Migration:** none — the sentinel is opt-in. Existing single-board
users who never reference `_default_` see zero change.

---

## Plan

### PR-A: Cross-host workspace payload sync

**Branch:** `feat/wave6-workspace-payload-sync`
**LOC:** ~500

1. Schema: `kanban_remote_hosts` gains `workspace_sync_enabled INTEGER NOT NULL DEFAULT 0`
2. New `opencomputer/kanban/workspace_payload.py`:
   - `pack_workspace(path, max_size) -> bytes` — gzip+tar, raises if > cap
   - `unpack_workspace(data, dest, max_size) -> None` — safe-extract via filter
3. `delegate_task_to_remote` extended:
   - When `task.workspace_kind == "dir"` and host has `workspace_sync_enabled`,
     pack + base64 + include in payload
4. Inbound `/proxy/spawn` extended:
   - When `workspace_payload_b64` present + this peer has sync enabled,
     unpack to a fresh `<kanban-root>/remote-workspaces/<task-id>/`
     and store that path as the local task's `workspace_path`
5. Inbound `/proxy/callback` extended:
   - When `workspace_payload_b64` present, sender unpacks into the
     original local task's `dir:<path>` location
6. Outbound: when peer's worker completes (locally on peer side), peer
   reads its own workspace path, packs, includes in callback POST
7. CLI: `oc kanban remote add ... --enable-workspace-sync` flag
8. Tests: 12+ (pack/unpack roundtrip, size cap, path-traversal rejection,
   spawn with payload, callback with payload, opt-out skips)

### PR-B: Legacy default board sentinel

**Branch:** `feat/wave6-default-board-sentinel`
**LOC:** ~150

1. Add `DEFAULT_BOARD_SLUG = "_default_"` constant in `db.py`
2. Update `_SLUG_RE` to allow leading underscore for the sentinel only:
   - Either widen the regex AND add a separate validator for user slugs
   - OR keep the regex strict + special-case the sentinel in `validate_slug`
3. Update `board_db_path("_default_")` → legacy path
4. Update `_would_cycle_global` to ingest the default board's
   task_links alongside named boards (use `DEFAULT_BOARD_SLUG` as the
   slug in the global edge map)
5. CLI: `oc kanban boards switch _default_` clears active marker
6. Tests: 6+ (sentinel validation, board_db_path mapping, cycle
   detection with default, regex correctly rejects leading underscore
   for non-sentinel)

---

## Self-audit

### A1. Tarfile extraction safety
Python 3.12 added `extractall(filter='data')` which rejects:
- Absolute paths
- Symlinks pointing outside the destination
- Device files

We require Python 3.12+ already (per pyproject.toml). Use the filter.

### A2. Payload size cap
50 MB hardcoded — anything bigger is asking for trouble. Document that
operator's load-balancer / proxy may have its own request-size cap;
50 MB stays under typical 100 MB defaults. Rejecting with 413 (Payload
Too Large).

### A3. Workspace sync is opt-in per host
The `workspace_sync_enabled` column means a peer that hasn't opted in
gets the existing 422 behavior. Sender must check the flag before
packing — saves work + bandwidth.

### A4. Bidirectional symmetry
Both sender → peer (spawn) AND peer → sender (callback) need the same
pack/unpack logic. Same module, same caps, same filter. Reuse.

### A5. Concurrent callbacks
Two callbacks from the same peer for different tasks could race on
disk extraction (different paths → safe). Same task duplicate callback
is rejected by the existing `find_claim_by_remote_id` check.

### A6. Atomic dir replacement on callback
When peer sends back modified workspace, we don't want a partial write
to corrupt the original. Strategy: extract to a sibling temp dir, then
`os.replace` the directory swap atomically. Restore old on failure.

Actually `os.replace` works on files but for directories we need a
different approach: extract to `<dir>.new`, rename old to `<dir>.old`,
rename new to `<dir>`, delete old. Rolling back is straightforward.

### A7. Sentinel slug regex compatibility
`_default_` doesn't match the existing `^[a-z0-9][a-z0-9_-]{0,63}$`
because it starts with `_`. Two clean approaches:
1. Special-case the sentinel before the regex check
2. Widen the regex to allow leading underscore + reject unrecognized
   leading-underscore slugs

Approach 1 is simpler. `validate_slug("_default_")` → pass without
regex check.

### A8. Existing tests
PR #458 has tests like `test_validate_slug_rejects_bad[_foo]` —
asserting `_foo` is rejected. With the sentinel, `_default_` is the
ONLY underscore-leading slug allowed. The existing rejection of `_foo`
still holds. No test regressions.

### A9. PR ordering
PR-A and PR-B touch different files; can land in either order.
Conservative: ship A first since it's bigger.

### A10. Cleanup
After both merge, prune local feature branches whose remote tip is
gone. Use `git fetch --prune` + `git branch -vv | grep gone`.

---

## Final plan summary

| PR | Title | LOC | Tests |
|---|---|---|---|
| A | Cross-host workspace payload sync | ~500 | 12+ |
| B | Legacy default board sentinel | ~150 | 6+ |
| C | Local branch cleanup (post-merge) | 0 | 0 |

Total ~650 LOC + 18+ tests. Execute now.
