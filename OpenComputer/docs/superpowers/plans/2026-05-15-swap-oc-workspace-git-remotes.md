# Swap oc-workspace Git Remotes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `sakshamzip2-sys/opencomputer-workspace` (Saksham's personal fork) the primary `origin` for `oc-workspace/`, demote `outsourc-e/hermes-workspace` to `upstream`, then push the current working `main` (Chats/Kanban/Design tabs + Chat-link removal + pnpm v11 fix) to the new origin.

**Architecture:** Pure git remote-config operation. No code changes. Three reversible config edits, one tag for recovery, then one force-push. After this, `git push` and `git pull` on `main` operate against the user's fork by default, and the upstream stays available for selective `git fetch upstream` if ever needed.

**Tech Stack:** git (CLI), the `oc-workspace/` repo at `/Users/saksham/Vscode/claude/OpenComputer/oc-workspace/`.

---

## Pre-flight invariants the executing agent MUST verify

Before any task, confirm in the oc-workspace repo:

```bash
cd /Users/saksham/Vscode/claude/OpenComputer/oc-workspace
git rev-parse --show-toplevel        # → /Users/saksham/Vscode/claude/OpenComputer/oc-workspace
git branch --show-current             # → main
git rev-parse HEAD                    # → 41d62b81... (or whatever the latest local main is)
git status -s                          # may show "?? pnpm-workspace.yaml" — OK, untracked artifact
git remote -v                          # MUST show BOTH `origin` (outsourc-e) AND `oc-workspace` (sakshamzip2-sys)
```

If any of these don't match, **STOP** and report — do not proceed.

---

### Task 1: Snapshot current state (recovery point)

**Files:** none (creates a git tag, local-only).

- [ ] **Step 1: Tag the current HEAD as a recovery point**

Run:
```bash
cd /Users/saksham/Vscode/claude/OpenComputer/oc-workspace
git tag pre-remote-swap-2026-05-15 HEAD
git tag --list pre-remote-swap-2026-05-15
```

Expected: prints `pre-remote-swap-2026-05-15`.

- [ ] **Step 2: Record current remote config to a file (audit trail)**

Run:
```bash
git remote -v > /tmp/oc-workspace-remotes-before-swap.txt
cat /tmp/oc-workspace-remotes-before-swap.txt
```

Expected output (4 lines):
```
oc-workspace	https://github.com/sakshamzip2-sys/opencomputer-workspace.git (fetch)
oc-workspace	https://github.com/sakshamzip2-sys/opencomputer-workspace.git (push)
origin	https://github.com/outsourc-e/hermes-workspace.git (fetch)
origin	https://github.com/outsourc-e/hermes-workspace.git (push)
```

If the output is different, STOP. The remaining tasks assume this exact starting state.

- [ ] **Step 3: Confirm GitHub auth is configured for the target repo**

Run:
```bash
git ls-remote https://github.com/sakshamzip2-sys/opencomputer-workspace.git HEAD 2>&1 | head -3
```

Expected: prints `<sha>\tHEAD` (a 40-char SHA). If you see `Authentication failed` or `403`, stop and tell the user to fix credentials before continuing.

---

### Task 2: Rename `origin` → `upstream`

**Files:** none (modifies `.git/config` only, fully reversible with `git remote rename`).

- [ ] **Step 1: Rename the upstream-pointing remote**

Run:
```bash
git remote rename origin upstream
```

Expected: no output (silent success).

- [ ] **Step 2: Verify**

Run:
```bash
git remote -v | grep upstream
```

Expected:
```
upstream	https://github.com/outsourc-e/hermes-workspace.git (fetch)
upstream	https://github.com/outsourc-e/hermes-workspace.git (push)
```

If `origin` still appears in `git remote -v`, the rename didn't apply — STOP.

---

### Task 3: Rename `oc-workspace` → `origin`

**Files:** none (`.git/config`).

- [ ] **Step 1: Rename the user's-fork remote to be the new primary**

Run:
```bash
git remote rename oc-workspace origin
```

Expected: no output. Internally, `git` rewrites the `branch.main.remote = oc-workspace` setting to `origin` if it existed — but `main` is currently tracking `upstream/main` (because that's where local main came from after the recent reset). We fix tracking in Task 4.

- [ ] **Step 2: Verify the new remote layout**

Run:
```bash
git remote -v
```

Expected output (exactly 4 lines, in any order):
```
origin	https://github.com/sakshamzip2-sys/opencomputer-workspace.git (fetch)
origin	https://github.com/sakshamzip2-sys/opencomputer-workspace.git (push)
upstream	https://github.com/outsourc-e/hermes-workspace.git (fetch)
upstream	https://github.com/outsourc-e/hermes-workspace.git (push)
```

If either line is wrong, STOP and tell the user.

---

### Task 4: Re-point `main` tracking to new `origin`

**Files:** none (`.git/config`).

- [ ] **Step 1: Check current tracking config**

Run:
```bash
git config branch.main.remote
git config branch.main.merge
```

Expected before fix: `upstream` and `refs/heads/main` (because main was originally checked out from `origin/main` which is now `upstream/main`).

- [ ] **Step 2: Fetch new origin so `origin/main` ref exists locally**

Run:
```bash
git fetch origin
```

Expected: pulls down branches from sakshamzip2-sys/opencomputer-workspace. Look for `* [new branch]      main       -> origin/main` or `From github.com:sakshamzip2-sys/opencomputer-workspace ... main`. If `fatal: couldn't find remote ref main` appears, the fork's main is empty — that's also fine, we'll create it in Task 5.

- [ ] **Step 3: Set tracking**

Run:
```bash
git branch --set-upstream-to=origin/main main
```

Expected: `Branch 'main' set up to track 'origin/main'.`

If `origin/main` doesn't exist yet (fork was empty), this command fails with `fatal: the requested upstream branch 'origin/main' does not exist`. In that case, SKIP this step — we'll run it AFTER Task 5's push.

- [ ] **Step 4: Verify**

Run:
```bash
git branch -vv | head -3
```

Expected: `* main 41d62b81 [origin/main: ahead N] fix(sidebar): remove Chat nav link...` (the `[origin/main]` part is the key — N ahead is normal because we just rewound).

---

### Task 5: Push local main to new origin

**Files:** none (only updates remote refs on sakshamzip2-sys/opencomputer-workspace).

- [ ] **Step 1: Push main with force-with-lease**

`--force-with-lease` is the safe force-push: aborts if someone else pushed to `origin/main` since we last fetched. We just fetched in Task 4 step 2, so the lease is fresh.

Run:
```bash
git push origin main --force-with-lease
```

Expected output contains `+ <old_sha>...<new_sha> main -> main (forced update)` OR `* [new branch] main -> main` if origin/main didn't exist before.

If you see `! [rejected] main -> main (stale info)`, someone else pushed — STOP and tell the user to investigate.
If you see `error: failed to push some refs` for other reasons — STOP, show the error to the user verbatim.

- [ ] **Step 2: If Task 4 step 3 was skipped because origin/main didn't exist, set tracking now**

Run:
```bash
git branch --set-upstream-to=origin/main main
```

Expected: `Branch 'main' set up to track 'origin/main'.`

Skip if Task 4 step 3 already succeeded.

- [ ] **Step 3: Verify push landed remotely**

Run:
```bash
git ls-remote origin main
```

Expected: prints `<sha>\trefs/heads/main` where `<sha>` matches `git rev-parse HEAD` locally.

Cross-check:
```bash
[ "$(git ls-remote origin main | cut -f1)" = "$(git rev-parse HEAD)" ] && echo MATCH || echo MISMATCH
```

Expected: `MATCH`. If `MISMATCH`, STOP.

---

### Task 6: Push the recovery tag

**Files:** none.

- [ ] **Step 1: Push the recovery tag to new origin**

The tag from Task 1 is local-only. Push it so the recovery point is also stored on GitHub.

Run:
```bash
git push origin pre-remote-swap-2026-05-15
```

Expected: `* [new tag]         pre-remote-swap-2026-05-15 -> pre-remote-swap-2026-05-15`.

- [ ] **Step 2: Verify**

Run:
```bash
git ls-remote --tags origin | grep pre-remote-swap
```

Expected: prints `<sha>\trefs/tags/pre-remote-swap-2026-05-15`.

---

### Task 7: Final verification

**Files:** none.

- [ ] **Step 1: Confirm final remote layout**

Run:
```bash
git remote -v
```

Expected:
```
origin	https://github.com/sakshamzip2-sys/opencomputer-workspace.git (fetch)
origin	https://github.com/sakshamzip2-sys/opencomputer-workspace.git (push)
upstream	https://github.com/outsourc-e/hermes-workspace.git (fetch)
upstream	https://github.com/outsourc-e/hermes-workspace.git (push)
```

- [ ] **Step 2: Confirm tracking**

Run:
```bash
git branch -vv | head -3
```

Expected: `* main <sha> [origin/main] fix(sidebar): remove Chat nav link...` — note `[origin/main]` with NO `ahead`/`behind` annotation (since we just pushed and main matches origin/main exactly).

- [ ] **Step 3: Confirm webui still healthy (sanity check)**

Run:
```bash
curl -sf --max-time 3 http://127.0.0.1:3000/api/connection-status | python3 -c "import sys,json;d=json.load(sys.stdin);print('status:', d.get('status'), 'model:', d.get('activeModel'))"
```

Expected: `status: enhanced model: claude-opus-4-7`.

This proves the running webui is unaffected by remote-config changes (it shouldn't be — only git config moved).

- [ ] **Step 4: Delete the `restore-old-ui` branch (cleanup, optional)**

The branch is now identical to `main` (same SHA `41d62b81`). Keeping it adds noise.

Run:
```bash
git branch -d restore-old-ui
```

Expected: `Deleted branch restore-old-ui (was 41d62b81).`

If git refuses with "not fully merged" error, that means it's NOT identical to main — STOP and report.

- [ ] **Step 5: Final state summary**

Run:
```bash
echo "=== FINAL STATE ==="
git remote -v
echo ""
git branch -vv
echo ""
git log --oneline -3
echo ""
echo "Recovery: git reset --hard pre-remote-swap-2026-05-15"
echo "Recovery on remote: git fetch origin pre-remote-swap-2026-05-15"
```

This is the report-back content.

---

## Rollback procedure (if anything goes wrong)

If push succeeded but turned out wrong:
```bash
cd /Users/saksham/Vscode/claude/OpenComputer/oc-workspace
git reset --hard pre-remote-swap-2026-05-15
git push origin main --force-with-lease
```

If remote rename was wrong but no push happened:
```bash
git remote rename origin oc-workspace
git remote rename upstream origin
```

If tracking is wrong:
```bash
git branch --set-upstream-to=upstream/main main   # back to old tracking
```

---

## What this plan does NOT change

- `chat-sidebar.tsx` content — already committed in `41d62b81`.
- `package.json` pnpm fix — already committed.
- The running webui at port 3000 — git config changes don't touch the served bundle.
- The `restore-old-ui` branch contents — only deletes the branch label (Task 7 step 4); the commit `41d62b81` remains on `main`.
- The upstream repo `outsourc-e/hermes-workspace` — we only DEMOTE it locally, never touch its remote state.
