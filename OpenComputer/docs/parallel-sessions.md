# Parallel Claude Sessions — Coordination Protocol

This repository has two Claude Code sessions running in parallel:

- **Session A** — executes the master plan at `~/.claude/plans/declarative-moseying-glade.md` (reference-parity + F-phases toward v2.0)
- **Session B** — executes the Hermes Self-Evolution plan at `~/.claude/plans/hermes-self-evolution-plan.md` (phases B1-B4, new `opencomputer/evolution/` subpackage)

Both sessions read this file at startup. Both update it after every commit.

---

## Rules (both sessions)

1. **Always branch.** Never push to `main`.
2. **Always PR.** The other session reviews before merge.
3. **Read this file at session start.** Check "Active working" below.
4. **Update this file after each commit.** One-line note listing files touched.
5. **Never merge while the other session's `Active working` touches a file you also touch.** Wait for the section to clear.

---

## Reserved files — Session A only (Session B must NOT modify)

- `opencomputer/agent/loop.py`
- `opencomputer/agent/memory.py`
- `opencomputer/agent/injection.py`
- `opencomputer/agent/config.py` *(new fields negotiated via PR review)*
- `plugin_sdk/*`

## Reserved files — Session B only (Session A must NOT modify)

- `opencomputer/evolution/*`
- `tests/test_evolution_*.py`
- `docs/evolution/*`
- `opencomputer/evolution/prompts/*.j2`

## Shared files (coordinate)

- `CLAUDE.md` — **Session A only** during scheduled refreshes. Session B flags changes needed via a PR to docs/evolution/README.md; Session A folds them in at next refresh.
- `CHANGELOG.md` — **both sessions append** under `## [Unreleased]`. Merge conflicts on concurrent writes are trivial (rebase + re-append).
- `pyproject.toml` — **both sessions** may add dependencies. Declare in PR description. Resolution: simple line-edit merge.
- `tests/` *(directory, not individual files)* — both sessions **add new files** freely. Neither modifies existing test files owned by the other session.

---

## Active working

> Sessions update this section after each commit. Keep entries terse (one line each).
> When a session is idle / between turns, remove its entries.

### Session A — [Idle / active]

*No active work entries yet. After each commit Session A writes:*
*`[YYYY-MM-DD HH:MM] <branch> touched <file-paths>`*

### Session B — active

- `[2026-04-24 13:45] feat/hermes-evolution-b1` touched `opencomputer/evolution/*` (new subpackage), `docs/evolution/*` (new), `tests/test_evolution_*.py` (new — 5 files / 73 tests), `docs/parallel-sessions.md` (this file), `CHANGELOG.md` (append [Unreleased] entry). **Zero changes to Session-A-reserved files.** Working from git worktree at `/tmp/oc-evo` to avoid branch-cycling conflicts with Session A in the primary checkout.
- `[2026-04-24 14:30] feat/hermes-evolution-b2` (stacked off `feat/hermes-evolution-b1`) touched `opencomputer/evolution/{reflect,synthesize,cli,entrypoint}.py`, `opencomputer/evolution/prompts/{reflect,synthesize}.j2`, `tests/test_evolution_{reflect_template,reflect_engine,synthesize_skill,cli}.py` (4 new test files / 36 new tests; -1 obsolete B1 stub test), `docs/evolution/README.md` (B1 placeholder → B2 user docs), `CHANGELOG.md` (append B2 entry under [Unreleased]). Full suite at 1070 passing. Worktree at `/tmp/oc-evo-b2`. **Zero Session-A-reserved files touched** (`opencomputer/cli.py` NOT modified — Session A wires the subapp via one-line PR per `docs/evolution/README.md`). **MERGED 2026-04-24 12:51 as `59ffa7c`** after #41 (B1) merged as `d2b13ac`.
- `[2026-04-24 18:30] feat/hermes-evolution-b4` touched `opencomputer/evolution/{prompt_evolution,monitor}.py` (new), `opencomputer/evolution/migrations/002_evolution_b4_tables.sql` (new), `opencomputer/evolution/{storage,cli}.py` (extended), `tests/test_evolution_{storage_b4,prompt_evolution,monitor,cli_b4}.py` (4 new test files / 58 new tests), `docs/evolution/README.md` (B2 status → B4 status), `CHANGELOG.md` (append B4 entry). Full suite at 1326 passing. Worktree at `/tmp/oc-evo-b4`. **B3 explicitly skipped** — depends on Session A's `opencomputer/ingestion/bus.py` which doesn't exist yet on main. **Zero Session-A-reserved files touched.**

---

## PR review responsibility

- **Session B opens PR → Session A reviews.** Session A verifies the PR touches only Session-B-reserved or shared files, nothing in Session A's reserved list.
- **Session A opens PR → Session B reviews** (in parallel-session mode; otherwise Session A self-merges per master plan's standard flow).
- If a PR accidentally touches the other session's reserved file, reviewer **rejects** and requests split.

---

## Bus API stability (Session A → B dependency)

Phase B3 of the Session B plan subscribes to Session A's TypedEvent bus at `opencomputer/ingestion/bus.py`. If Session A needs to make a **breaking change** to the bus public API (event schema, subscriber contract), announce it here:

### Bus API change log

*Format: `[YYYY-MM-DD] <change> — <migration-note-for-Session-B>`*

*None yet — Session A has not yet shipped F2 TypedEvent bus.*

---

## Rollback / emergency

If a cross-session conflict lands on main and breaks tests:

1. First session to notice: revert the latest offending commit on a new branch, open PR labeled `hotfix:`.
2. Update `CHANGELOG.md` under `## [Unreleased]` with the revert note.
3. Post a one-line summary in Active Working so the other session knows.

---

## Coordination meta-rules

- **Session A has precedence on merges** during Phase 2/3 foundations (F1 inherit + signal normalizer). Session B defers if both merge-ready simultaneously.
- **Session B has precedence on merges** during its own Phase B2 (skill synthesis) — that's a self-contained window.
- **If in doubt, pause.** Both sessions can afford to wait 10-30 minutes for the other to finish a merge; neither can afford a broken main.

---

## Last updated

- **Plan files linked:**
  - `~/.claude/plans/declarative-moseying-glade.md` (Session A master plan)
  - `~/.claude/plans/hermes-self-evolution-plan.md` (Session B plan)
- **This protocol:** 2026-04-24
