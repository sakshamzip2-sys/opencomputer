# SQLite Organization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce one canonical Markdown reference (`OpenComputer/docs/databases.md`) that maps every SQLite file, table, owner module, and convention quirk in the codebase, without changing a single line of Python, SQL, JSON, or TOML.

**Architecture:** Documentation-only. Read each owner module to extract DDL summaries; assemble into one ~600-line file; verify every claim with `git grep` before commit; optionally land a 1-line pointer in `OpenComputer/CLAUDE.md` so future sessions auto-discover the doc. Linked spec: `OpenComputer/docs/superpowers/specs/2026-05-06-sqlite-organization-design.md`.

**Tech Stack:** Markdown only. The "tests" are `git diff` invariants (no code touched) plus claim verifications (`git grep` confirms every file path and table name in the doc maps back to source).

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `OpenComputer/docs/databases.md` | **Create** | The canonical SQLite reference (the deliverable). |
| `OpenComputer/docs/superpowers/specs/2026-05-06-sqlite-organization-design.md` | (already exists) | Companion design spec — referenced from the doc footer. |
| `OpenComputer/CLAUDE.md` | **Modify (optional)** | One-line pointer under §9 ("If you need to dig deeper") — only if user approves the optional pointer. |
| Everything else | **Untouched** | `git diff -- '*.py' '*.sql' '*.json' '*.toml'` must remain empty after this work. |

---

## Pre-flight invariants (read before starting any task)

These hold for the entire plan:

- **No code edits.** No `*.py`, `*.sql`, `*.json`, `*.toml`, `*.yaml`. Only the new `databases.md` and (optionally) one line in `CLAUDE.md`.
- **No new DB files.** Don't create `.db` or `.sqlite`. Don't even create empty ones.
- **No table changes.** No `CREATE`, `ALTER`, `DROP`, or migration version bump anywhere.
- **No row movement.** Zero data migration.
- **Verify, don't memorize.** Every claim in the doc (path, table name, schema_version constant, owner module) is verified with `git grep` or `Read` before committing the section.
- **Commit per section.** One commit per major section keeps the diff easy to review and lets the user roll back any one section without losing the rest.

---

## Task 1: Branch + scratch directory setup

**Files:** none (git only)

- [ ] **Step 1.1: Confirm working tree is clean of uncommitted edits**

Run:

```bash
cd /Users/saksham/Vscode/claude
git status --short
```

Expected: only the new spec file (`?? OpenComputer/docs/superpowers/specs/2026-05-06-sqlite-organization-design.md`) and the existing untracked items (`.claude/`, `codeburn.VMgZ/`, `evals/`, `package-lock.json`). No staged or unstaged tracked-file modifications.

- [ ] **Step 1.2: Create a feature branch**

Run:

```bash
cd /Users/saksham/Vscode/claude
git checkout -b docs/sqlite-organization-map
```

Expected: branch switches successfully. The new spec file follows the branch.

- [ ] **Step 1.3: Stage + commit the spec on this branch**

Run:

```bash
git add OpenComputer/docs/superpowers/specs/2026-05-06-sqlite-organization-design.md
git commit -m "docs(spec): SQLite organization map (no schema changes)

Adds a design spec for a documentation-only catalog of every SQLite
file, table, and migration in OpenComputer. The deliverable will be
a single docs/databases.md; this commit lands only the spec."
```

Expected: 1 file committed.

---

## Task 2: Re-verify the SQLite landscape (catch anything the spec missed)

**Files:** read-only — no writes

- [ ] **Step 2.1: List every `.db` / `.sqlite*` file in the repo**

Run from `/Users/saksham/Vscode/claude`:

```bash
find . -name "*.db" -not -path "*/node_modules/*" -not -path "*/.git/*" -not -path "*/.venv/*" 2>/dev/null
find . -name "*.sqlite*" -not -path "*/node_modules/*" -not -path "*/.git/*" -not -path "*/.venv/*" 2>/dev/null
```

Expected output (from the brainstorm):

```
./evals/history.db
./OpenComputer/evals/history.db
./sources/openclaw-2026.4.23/src/tasks/task-registry.store.sqlite.ts
./sources/openclaw-2026.4.23/src/tasks/task-flow-registry.store.sqlite.ts
./sources/openclaw-2026.4.23/src/proxy-capture/store.sqlite.ts
./sources/openclaw-2026.4.23/src/proxy-capture/store.sqlite.test.ts
./sources/openclaw/src/tasks/task-registry.store.sqlite.ts
./sources/openclaw/src/tasks/task-flow-registry.store.sqlite.ts
./sources/openclaw/src/proxy-capture/store.sqlite.ts
./sources/openclaw/src/proxy-capture/store.sqlite.test.ts
./sources/openclaw/src/plugin-state/plugin-state-store.sqlite.ts
```

Note: the `.ts` files under `sources/` are reference-repo TypeScript stores, NOT ours. The only Python-owned `.db` files on-disk currently are `./evals/history.db` (parent) and `./OpenComputer/evals/history.db` (project). Profile-scoped DBs live under `~/.opencomputer/<profile>/` (outside the repo).

If anything new appears, **stop** and update the spec before continuing.

- [ ] **Step 2.2: Catalog every `CREATE TABLE` and `CREATE INDEX` in production code**

Run:

```bash
grep -rEn "CREATE TABLE|CREATE INDEX" /Users/saksham/Vscode/claude/OpenComputer \
  --include="*.py" 2>/dev/null \
  | grep -v __pycache__ \
  | grep -v "\.venv/" \
  | grep -v "/tests/" \
  | sort
```

Expected: matches across `agent/state.py`, `tasks/store.py`, `gateway/outgoing_queue.py`, `plugins/demand_tracker.py`, `evals/history.py`, `evolution/rate_limit.py`, `evolution/storage.py` (via SQL files), `inference/storage.py`, `user_model/store.py`, `user_model/drift_store.py`, `kanban/db.py`, `extensions/coding-harness/tools/todo_write.py`. If you see a Python file in production code with `CREATE TABLE` that's NOT in the spec's owner list, **stop** and update the spec.

- [ ] **Step 2.3: Catalog every `sqlite3.connect()` site**

Run:

```bash
grep -rEn "sqlite3\.connect" /Users/saksham/Vscode/claude/OpenComputer \
  --include="*.py" 2>/dev/null \
  | grep -v __pycache__ \
  | grep -v "\.venv/" \
  | grep -v "/tests/" \
  | sort
```

Expected: ~25 sites. Each maps to one of the 8 documented DB files (or to the `_resolve_db_path()` indirection used by `tools/send_message.py`, `tools/spawn_detached_task.py`, etc., which all resolve to `sessions.db`). If you see a connect call to a path that's not in the catalog, **stop** and update.

- [ ] **Step 2.4: Sanity-check for the stale `consent/audit.db` reference**

Run:

```bash
grep -rEn "audit\.db" /Users/saksham/Vscode/claude/OpenComputer \
  --include="*.py" 2>/dev/null | grep -v __pycache__ | grep -v "\.venv/"
```

Expected: only `opencomputer/cli_backup.py:197` (the staged-restore path). If more appear, **stop** and update.

- [ ] **Step 2.5: Note the verification (no commit; this is a read-only step)**

Verification done. Proceed to Task 3.

---

## Task 3: Write the doc skeleton — header, TL;DR, table of contents, ASCII map

**Files:**
- Create: `OpenComputer/docs/databases.md`

- [ ] **Step 3.1: Create the file with header + TL;DR + ToC**

Write `OpenComputer/docs/databases.md` with this exact content:

````markdown
# OpenComputer SQLite databases — canonical map

> **What this is.** A single reference for every SQLite file the
> OpenComputer codebase owns, what's inside, who writes to it, and how
> migrations work for that DB. Read this when you're asking "where is X
> stored?" or "is feature Y profile-scoped?" or "why are there two
> `tasks` tables?"
>
> **What this is NOT.** A column-by-column schema dump (use `git grep
> "CREATE TABLE"` for that), a migration history (see CHANGELOG.md and
> in-code comments), or a refactor proposal. This document maps the
> existing layout. The companion design spec at
> `docs/superpowers/specs/2026-05-06-sqlite-organization-design.md`
> records the rationale.

## TL;DR

OpenComputer ships **8 SQLite files**. One per-profile **megastore**
(`sessions.db`) holds 19 SQLite objects across 5 owner modules. Five
sub-DBs sit beside it under `<profile_home>/`. Two DBs sit *outside*
any profile by design: `kanban.db` (cross-profile coordination
primitive), `evals/history.db` (cwd-scoped, project-local).

| Layer | File | Path formula | Profile-scoped? |
|---|---|---|---|
| Megastore | `sessions.db` | `<profile_home>/sessions.db` | yes |
| Evolution | `evolution/trajectory.sqlite` | `<profile_home>/evolution/trajectory.sqlite` | yes |
| Evolution | `evolution/rate.db` | `~/.opencomputer/evolution/rate.db` | **no** (gotcha — see §4.2) |
| Inference | `inference/motifs.sqlite` | `<profile_home>/inference/motifs.sqlite` | yes |
| User model | `user_model/graph.sqlite` | `<profile_home>/user_model/graph.sqlite` | yes |
| User model | `user_model/drift_reports.sqlite` | `<profile_home>/user_model/drift_reports.sqlite` | yes |
| Coordination | `kanban.db` | `<oc_root>/kanban.db` (or `<oc_root>/kanban/boards/<slug>/kanban.db`) | **shared by design** |
| Evaluation | `evals/history.db` | `$OPENCOMPUTER_EVAL_HISTORY_DB` or `$CWD/evals/history.db` | **no** (cwd-scoped) |

## Table of contents

1. [Filesystem map](#1-filesystem-map)
2. [The megastore: `sessions.db`](#2-the-megastore-sessionsdb)
3. [Profile-scoped sub-DBs](#3-profile-scoped-sub-dbs)
4. [Cross-profile and non-profile DBs](#4-cross-profile-and-non-profile-dbs)
5. [External DBs we read but don't own](#5-external-dbs-we-read-but-dont-own)
6. [Conventions audit (the "all over the place" callout)](#6-conventions-audit)
7. [Reading the code](#7-reading-the-code)
8. [Future cleanup candidates (parked)](#8-future-cleanup-candidates)
````

- [ ] **Step 3.2: Append the §1 filesystem map**

Append to `OpenComputer/docs/databases.md`:

````markdown

---

## 1. Filesystem map

```
~/.opencomputer/                                  ← OC root (overridable via OC_HOME)
│
├── kanban.db                                     ← cross-profile coordination (shared)
├── kanban/
│   ├── .active-board                             ← single-line text: active slug
│   ├── boards/<slug>/kanban.db                   ← per-named-board (multi-board)
│   ├── boards/<slug>/workspaces/                 ← per-board scratch dirs
│   └── workspaces/                               ← legacy single-board scratch
│
├── evolution/
│   └── rate.db                                   ← shared rate limiter (NOT per-profile;
│                                                    profile-aware brethren are below
│                                                    under <profile>/evolution/)
│
└── <profile>/                                    ← e.g. "default", "work", "saksham"
    ├── sessions.db                               ← MEGASTORE — see §2
    ├── config.yaml                               ← per-profile settings
    ├── profile.yaml                              ← active plugins / preset
    │
    ├── evolution/
    │   └── trajectory.sqlite                     ← RL-style training trajectories
    │
    ├── inference/
    │   └── motifs.sqlite                         ← inferred behavioural motifs
    │
    └── user_model/
        ├── graph.sqlite                          ← F4 user-model nodes/edges + FTS5
        └── drift_reports.sqlite                  ← decay/drift report archive
```

**Project-local (not under `~`):**

```
<repo_root>/
└── evals/
    └── history.db                                ← eval-harness run history (cwd-scoped)
```

**Path-helper cheat sheet:**

| Module | Helper | Returns |
|---|---|---|
| `agent/config.py` | `_home()` | `<oc_root>/<active_profile>/` |
| `agent/config.py` | `default_config().home` | same as `_home()` for the default profile |
| `agent/config.py` | `cfg.session.db_path` | `_home() / "sessions.db"` |
| `kanban/db.py` | `kanban_home()` | `<oc_root>/` (one level above profile, by design) |
| `kanban/db.py` | `kanban_db_path()` | resolves env overrides → active board → legacy default |
| `evolution/storage.py` | `evolution_home()` | `_home() / "evolution"` (per-profile — distinct from rate.db's path) |

**Profile resolution.** `_home()` returns `<oc_root>/<profile>/` where
`oc_root = OC_HOME or ~/.opencomputer` and `profile = OC_PROFILE or "default"`.
The `oc -p <profile>` CLI flag exports `OC_PROFILE` before any imports run.
````

- [ ] **Step 3.3: Verify all paths in the map are real**

Run:

```bash
grep -n "_home()" /Users/saksham/Vscode/claude/OpenComputer/opencomputer/agent/config.py | head -5
grep -n "kanban_home\|kanban_db_path\|evolution_home" \
  /Users/saksham/Vscode/claude/OpenComputer/opencomputer/kanban/db.py \
  /Users/saksham/Vscode/claude/OpenComputer/opencomputer/evolution/storage.py | head -10
```

Expected: every helper named in the cheat sheet exists at the cited module:line. If any helper is missing, **stop** and fix the doc.

- [ ] **Step 3.4: Commit Task 3**

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/docs/databases.md
git commit -m "docs(databases): add header, TL;DR, filesystem map (§1)"
```

---

## Task 4: Write §2 — the megastore (`sessions.db`)

**Files:** Modify `OpenComputer/docs/databases.md` (append §2)

- [ ] **Step 4.1: Append §2 to the doc**

Append:

````markdown

---

## 2. The megastore: `sessions.db`

**Path:** `<profile_home>/sessions.db`
**Owner:** `opencomputer.agent.state.SessionDB`
**Schema-version constant:** `SCHEMA_VERSION` at `opencomputer/agent/state.py:37` (currently 12 at time of writing — check the constant for live value).
**Migration style:** numbered Python migrations dict `MIGRATIONS: dict[tuple[int, int], str]` + single-row `schema_version` table. See `apply_migrations()` at `opencomputer/agent/state.py`.
**Concurrency:** WAL mode + application-level retry-with-jitter on `SQLITE_BUSY`.

### Why one DB and not many

`tasks/store.py` documents the rationale: *"Same DB as sessions —
keeps everything in one file per profile so `opencomputer profile
delete` cleans up cleanly. No second DB to worry about."* The
megastore is a load-bearing simplification, not an accident.

### Tables declared in `agent/state.py` (13 base + 2 FTS5)

| Table | Purpose | Schema-version added | Notes |
|---|---|---|---|
| `schema_version` | Single-row counter for migrations. | v0 (baseline) | One INTEGER NOT NULL row. |
| `sessions` | One row per conversation. | v1 | Adds `vibe`/`vibe_updated` (v6), `cwd` (Plan 3), `goal_*` (v11). |
| `messages` | One row per turn. | v1 | `reasoning_details`/`codex_reasoning_items`/`reasoning_replay_blocks`/`attachments` added v2. |
| `messages_fts` | FTS5 virtual table mirroring `messages.content`. | v1 | Tokenizer changed to `trigram` in v12 (CJK + substring search); falls back to `porter unicode61` if SQLite build lacks trigram. |
| `episodic_events` | Per-turn event summaries (denormalized for "remind me what we decided about X"). | v1 | `dreamed_into` added v4 (P-18 dreaming consolidation); `recall_penalty` + `recall_penalty_updated_at` added v9. |
| `episodic_fts` | FTS5 virtual table mirroring `episodic_events.summary`/`tools_used`/`file_paths`. | v1 | Tokenizer: `porter unicode61`. |
| `consent_grants` | F1 consent layer — granted capabilities per scope. | v3 | PK `(capability_id, scope_filter)`. |
| `consent_counters` | F1 consent layer — clean-run counters for progressive promotion. | v3 | PK `(capability_id, scope_filter)`. |
| `audit_log` | F1 immutable HMAC-chained audit. | v3 | UPDATE/DELETE blocked by triggers (tamper-EVIDENCE not tamper-proof; FS edits caught by `AuditLogger.verify_chain()`). |
| `tool_usage` | Per-tool-call telemetry powering `opencomputer insights`. | v5 | Indexes on session, ts DESC, tool. |
| `vibe_log` | Per-message persona/vibe verdict log (classifier evidence). | v6 | `classifier_version` field for A/B between regex and embedding/LLM classifiers. |
| `turn_outcomes` | Outcome-aware learning: per-turn implicit signals + scoring. | v7 (signals), v8 (scores) | Phase 0 lands signals; Phase 1 layers `composite_score`/`judge_score`/`turn_score`. |
| `recall_citations` | Outcome-aware learning: which memories were surfaced for which turn. | v7 | Joined with `turn_outcomes` to compute "memory M's downstream score." |
| `policy_changes` | Reversible policy decisions, HMAC-chained as drafted. | v9 | Status: drafted → pending → active → reverted/expired_decayed. |
| `policy_audit_log` | Append-only HMAC-chained log of every policy status transition. | v10 | v0.5 of outcome-aware learning closed v0's "transitions = UPDATEs" deferral. |

### Tables attached by other modules (4 tables — `CREATE TABLE IF NOT EXISTS`, no schema-version coordination)

| Table | Purpose | Owner module | Indexes |
|---|---|---|---|
| `tasks` | Detached LLM-task CRUD (queued/running/done/failed/cancelled/orphaned). | `opencomputer/tasks/store.py` | `idx_tasks_status`, `idx_tasks_session` |
| `outgoing_messages` | Bridges processes (`mcp serve`, gateway) that need to send platform messages. | `opencomputer/gateway/outgoing_queue.py` | `idx_outgoing_status` |
| `plugin_demand` | Tool-not-found demand signals for installed-but-disabled plugins (Sub-project E). | `opencomputer/plugins/demand_tracker.py` | `plugin_demand_by_plugin` |
| `session_state` | TodoWrite key/value persistence (only extension-contributed table). | `extensions/coding-harness/tools/todo_write.py` | PK `(session_id, key)` |

> **Same name, different DB:** `sessions.db` has a `tasks` table for
> detached LLM tasks; `kanban.db` *also* has a `tasks` table for
> kanban tickets. Same word, completely different schemas, completely
> different files. See §6 for why this isn't actually confusing.

### How a Claude session reads/writes the megastore

The canonical accessor is `cfg.session.db_path` (a `pathlib.Path`
attribute on `agent.config.SessionConfig`). Every CLI subcommand that
needs the megastore should accept it via `cfg`, not call `_home()`
directly. ~10 modules historically inlined `_home() / "sessions.db"` —
`opencomputer/cli_pair.py:81`, `opencomputer/cli_consent.py:58`,
`opencomputer/cli_dashboard.py:71`, `opencomputer/cli_session.py:61`,
`opencomputer/mcp/server.py` (multiple sites), and others. They all
resolve to the same file but bypass test-time injection of
`cfg.session.db_path`.
````

- [ ] **Step 4.2: Verify the schema-version constant cited**

Run:

```bash
grep -n "^SCHEMA_VERSION" /Users/saksham/Vscode/claude/OpenComputer/opencomputer/agent/state.py
```

Expected: `37:SCHEMA_VERSION = 12`. If the line number drifted, update the doc.

- [ ] **Step 4.3: Verify every table name listed actually exists**

Run:

```bash
for t in schema_version sessions messages messages_fts episodic_events episodic_fts consent_grants consent_counters audit_log tool_usage vibe_log turn_outcomes recall_citations policy_changes policy_audit_log; do
  echo -n "$t: "
  grep -l "CREATE TABLE.*$t\|CREATE VIRTUAL TABLE.*$t" /Users/saksham/Vscode/claude/OpenComputer/opencomputer/agent/state.py | head -1 || echo "MISSING"
done
```

Expected: every table maps to `agent/state.py`. If any "MISSING" appears, fix the doc.

```bash
for t in tasks outgoing_messages plugin_demand session_state; do
  echo -n "$t: "
  grep -l "CREATE TABLE.*$t" /Users/saksham/Vscode/claude/OpenComputer/opencomputer/tasks/store.py /Users/saksham/Vscode/claude/OpenComputer/opencomputer/gateway/outgoing_queue.py /Users/saksham/Vscode/claude/OpenComputer/opencomputer/plugins/demand_tracker.py /Users/saksham/Vscode/claude/OpenComputer/extensions/coding-harness/tools/todo_write.py 2>/dev/null | head -1 || echo "MISSING"
done
```

Expected: every attached table maps to its module. Fix if not.

- [ ] **Step 4.4: Commit Task 4**

```bash
git add OpenComputer/docs/databases.md
git commit -m "docs(databases): add §2 megastore (sessions.db) catalog"
```

---

## Task 5: Write §3 — profile-scoped sub-DBs

**Files:** Modify `OpenComputer/docs/databases.md` (append §3)

- [ ] **Step 5.1: Append §3 to the doc**

Append:

````markdown

---

## 3. Profile-scoped sub-DBs

Five DBs live alongside `sessions.db` under `<profile_home>/`. Each
one is small and focused, with its own ownership and migration style.

### 3.1 `evolution/trajectory.sqlite`

**Path:** `<profile_home>/evolution/trajectory.sqlite`
**Owner:** `opencomputer/evolution/storage.py` (`trajectory_db_path()`)
**Tables:** `schema_version`, `trajectory_*` (per migration files).
**Migration style:** discovered SQL files at
`opencomputer/evolution/migrations/*.sql`. The runner glob-loads files
matching `^(\d+)_.+\.sql$`, sorts by numeric prefix, and applies any
above the stored `MAX(version)`. Each migration runs in its own
transaction. As of writing, three SQL files exist: `001_evolution_initial.sql`,
`002_evolution_b4_tables.sql`, `003_evolution_cache_warning.sql`.
**Why this is unique:** the only DB in the codebase using SQL files
instead of inline Python migrations. Documented in `evolution/design.md`
§5.1; flagged for future unification with the F1 framework
(`# TODO(F1)` at `evolution/storage.py:5`).

### 3.2 `evolution/rate.db` — the path-not-profile-scoped gotcha

**Path:** `~/.opencomputer/evolution/rate.db` (HARDCODED — does NOT
honor `OC_HOME` or the active profile).
**Owner:** `opencomputer/evolution/rate_limit.py` (`DraftRateLimiter`)
**Tables:** `drafts(iso_ts TEXT PRIMARY KEY)` — one row per
successfully synthesized draft.
**Migration style:** none. Just `CREATE TABLE IF NOT EXISTS`.
**Caps:** per-day default 1, lifetime default 10. Reset via
`opencomputer skill reset-limits`.

> **Gotcha.** The default path is `Path.home() / ".opencomputer" /
> "evolution" / "rate.db"`. If a user runs OpenComputer with
> `OC_HOME=/opt/oc` (Docker / custom deployment) the rate counter
> still writes to `~/.opencomputer/evolution/rate.db` rather than
> `/opt/oc/evolution/rate.db`. Compare to `evolution/trajectory.sqlite`
> in the same module, which correctly uses `_home()`. This
> inconsistency is documented intentionally — fixing it is parked
> under §8.

### 3.3 `inference/motifs.sqlite`

**Path:** `<profile_home>/inference/motifs.sqlite`
**Owner:** `opencomputer/inference/storage.py` (`MotifStore`)
**Tables:** `schema_version`, `motifs` (PK `motif_id`; indexes on
`(kind, created_at DESC)`).
**Migration style:** numbered Python dict
`MIGRATIONS: dict[tuple[int, int], str]` (mirrors `agent/state.py`).
Current schema_version: 1.
**Read by:** `user_model/graph.sqlite`'s motif importer
(`opencomputer/user_model/importer.py`) — Phase 4.A schema groundwork.

### 3.4 `user_model/graph.sqlite`

**Path:** `<profile_home>/user_model/graph.sqlite`
**Owner:** `opencomputer/user_model/store.py` (`UserModelStore`)
**Tables:** `schema_version`, `nodes`, `edges`, `nodes_fts` (FTS5).
**Migration style:** numbered Python dict. Current schema_version: 2
(`edges.source` provenance column added v2 for the F4 + Honcho hybrid;
see `docs/memory-architecture.md` for the cycle-prevention rationale).
**FTS5 tokenizer:** `porter unicode61` on `nodes.value`.
**Companion doc:** `docs/memory-architecture.md` covers the F4 + Honcho
hybrid contract; this section is the storage view.

### 3.5 `user_model/drift_reports.sqlite`

**Path:** `<profile_home>/user_model/drift_reports.sqlite`
**Owner:** `opencomputer/user_model/drift_store.py` (`DriftStore`)
**Tables:** `schema_version`, `drift_reports`.
**Migration style:** numbered Python dict. Current schema_version: 1.
**Retention:** opt-in via `DriftStore.delete_older_than()`.
**Written by:** `DriftDetector.detect()` when a store is attached
(Phase 3.D).
````

- [ ] **Step 5.2: Verify each sub-DB path formula**

Run:

```bash
grep -n "_default_db_path\|trajectory_db_path\|drift_reports.sqlite\|motifs.sqlite\|graph.sqlite" \
  /Users/saksham/Vscode/claude/OpenComputer/opencomputer/evolution/storage.py \
  /Users/saksham/Vscode/claude/OpenComputer/opencomputer/inference/storage.py \
  /Users/saksham/Vscode/claude/OpenComputer/opencomputer/user_model/store.py \
  /Users/saksham/Vscode/claude/OpenComputer/opencomputer/user_model/drift_store.py | head -20
```

Expected: every path formula matches the doc. Fix any drift.

- [ ] **Step 5.3: Verify the rate.db hardcoded path claim**

Run:

```bash
grep -n "rate.db\|Path.home" /Users/saksham/Vscode/claude/OpenComputer/opencomputer/evolution/rate_limit.py
```

Expected: line 37 shows `self.db_path = db_path or Path.home() / ".opencomputer" / "evolution" / "rate.db"`. Confirms the gotcha. If the line moved, update the doc.

- [ ] **Step 5.4: Commit Task 5**

```bash
git add OpenComputer/docs/databases.md
git commit -m "docs(databases): add §3 profile-scoped sub-DBs (5 DBs)"
```

---

## Task 6: Write §4 — cross-profile and non-profile DBs

**Files:** Modify `OpenComputer/docs/databases.md` (append §4)

- [ ] **Step 6.1: Append §4 to the doc**

Append:

````markdown

---

## 4. Cross-profile and non-profile DBs

Two DBs deliberately do NOT live under any single profile.

### 4.1 `kanban.db` — cross-profile coordination primitive

**Path resolution (precedence highest → lowest):**

1. `OC_KANBAN_DB` env (explicit pin)
2. `OC_KANBAN_BOARD` env / `<oc_root>/kanban/.active-board` state file → per-board path at `<oc_root>/kanban/boards/<slug>/kanban.db`
3. Legacy unnamed default at `<oc_root>/kanban.db`

`<oc_root>` resolves via `OC_KANBAN_HOME` env or `_oc_home()`. Note: NOT `_home()`
— `kanban_home()` deliberately resolves one level above the profile so
all profiles share the board (see module docstring for the dispatcher/worker
handoff rationale).

**Owner:** `opencomputer/kanban/db.py`
**Migration style:** none — `CREATE TABLE IF NOT EXISTS` plus an
additive in-place migration in `init_db()` for new columns
(`idx_tasks_idempotency`, `idx_events_run`, etc.).

**Tables (11):**

| Table | Purpose |
|---|---|
| `tasks` | One row per kanban ticket (status, assignee, claim_lock, workspace_kind/path, idempotency_key, skills, …). |
| `task_links` | Parent/child task graph; supports cross-board links via `parent_board`/`child_board` (Wave 6.E.10). |
| `task_comments` | Threaded comments (author, body, created_at). |
| `task_events` | Event log per task (status changes, claim/release, completion). |
| `task_runs` | Historical attempt records — claim/PID/heartbeat/runtime cap/summary lives here, NOT on `tasks`. |
| `kanban_notify_subs` | Gateway subscription `(platform, chat_id, thread_id)` → task notifications. |
| `kanban_remote_hosts` | Wave 6.E.13 — registered peers for multi-host write coordination. |
| `kanban_remote_claims` | Wave 6.E.13 — pending tasks delegated to a remote host. |
| `kanban_assignment_rules` | Wave 6.E.9 — auto-assignment routing rules (`title_regex`, `tenant`, `default`). |
| `kanban_delegated_tasks` | Wave 6.E.17 — peer-side mirror of incoming `/proxy/spawn` requests. |
| `kanban_pending_callbacks` | Wave 6.E.17 — outbound callback retry queue with exponential backoff. |

**Why cross-profile by design.** A worker spawned with
`oc -p other-profile` must join the SAME board as the dispatcher. If
each profile had its own `kanban.db`, the dispatcher/worker handoff
would silently fork. The module-level docstring at `kanban/db.py:1-38`
locks this in.

### 4.2 `evals/history.db` — cwd-scoped, project-local

**Path resolution (precedence highest → lowest):**

1. `OPENCOMPUTER_EVAL_HISTORY_DB` env (explicit pin)
2. Default `<cwd>/evals/history.db`

**Owner:** `opencomputer/evals/history.py`
**Migration style:** none — `CREATE TABLE IF NOT EXISTS`.
**Tables (1):** `eval_runs`. Columns: `site_name`, `timestamp`, `accuracy`,
`correct`, `incorrect`, `parse_failures`, `infra_failures`, `total`,
`model`, `provider`, `grader_model`, `cost_usd`, `input_tokens`,
`output_tokens`, `case_runs_json`. Index on `(site_name, timestamp DESC)`.
**Retention:** enforced at write time, default 100 runs per site.

**Why cwd-scoped.** Eval reproducibility happens at the project level
(you `cd` into a checked-out repo to rerun an eval). User-level
profile scoping would couple eval history to whichever profile
happened to be active — wrong layer.
````

- [ ] **Step 6.2: Verify kanban paths**

Run:

```bash
grep -n "kanban_home\|kanban_db_path\|board_db_path\|OC_KANBAN" \
  /Users/saksham/Vscode/claude/OpenComputer/opencomputer/kanban/db.py | head -20
```

Expected: matches the precedence order in the doc. If it drifted, fix the doc.

- [ ] **Step 6.3: Verify eval path resolution**

Run:

```bash
grep -n "OPENCOMPUTER_EVAL_HISTORY_DB\|history.db" \
  /Users/saksham/Vscode/claude/OpenComputer/opencomputer/cli_eval.py \
  /Users/saksham/Vscode/claude/OpenComputer/opencomputer/evals/history.py | head -10
```

Expected: env var name and default path match.

- [ ] **Step 6.4: Commit Task 6**

```bash
git add OpenComputer/docs/databases.md
git commit -m "docs(databases): add §4 cross-profile + non-profile DBs (kanban, evals)"
```

---

## Task 7: Write §5 — external DBs we read but don't own

**Files:** Modify `OpenComputer/docs/databases.md` (append §5)

- [ ] **Step 7.1: Append §5 to the doc**

Append:

````markdown

---

## 5. External DBs we read but don't own

| DB | Path | Used by | Read/write |
|---|---|---|---|
| macOS Messages (`chat.db`) | `~/Library/Messages/chat.db` (overridable via `IMESSAGE_DB_PATH`) | `opencomputer/skills/profile_scraper/scraper.py` | **Read-only.** Source for the iMessage scraper skill that bootstraps user-profile data. |

**Notes:**

- These are **macOS-only**. The scraper does nothing on Linux/Windows.
- We never write to them.
- The setup-wizard surfaces `IMESSAGE_DB_PATH` as one of the
  per-platform env-var keys (`opencomputer/setup_wizard.py`).
- The `imessage` channel adapter and `iMessage` skill toolkit are
  separate concerns; this entry is about the macOS Messages chat DB.
````

- [ ] **Step 7.2: Verify the external DB path is referenced exactly once for reading**

Run:

```bash
grep -rEn "Library/Messages/chat\.db" /Users/saksham/Vscode/claude/OpenComputer --include="*.py" 2>/dev/null | grep -v __pycache__
```

Expected: at least one match in `skills/profile_scraper/scraper.py`. If you find write paths, **stop** and update the doc.

- [ ] **Step 7.3: Commit Task 7**

```bash
git add OpenComputer/docs/databases.md
git commit -m "docs(databases): add §5 external DBs (macOS Messages chat.db)"
```

---

## Task 8: Write §6 — conventions audit

**Files:** Modify `OpenComputer/docs/databases.md` (append §6)

- [ ] **Step 8.1: Append §6 to the doc**

Append:

````markdown

---

## 6. Conventions audit

The following inconsistencies are **observed**, not "fixed" by this
document. Each one is intentional or historical; this section makes
them visible so future work doesn't reintroduce variations.

### 6.1 Two file extensions for the same purpose

| `.db` | `.sqlite` |
|---|---|
| sessions.db | trajectory.sqlite |
| kanban.db | motifs.sqlite |
| evals/history.db | graph.sqlite |
| evolution/rate.db | drift_reports.sqlite |
| (`consent/audit.db` — legacy restore-only path, see §6.6) | |

No principle distinguishes them. The newer F4/inference/decay storage
modules (Phase 3 onward) used `.sqlite`; older modules used `.db`.
Normalizing is parked in §8.

### 6.2 Six path-resolution helpers

Each owner module evolved its own path resolver. They all converge on
correct paths but the diversity is a maintenance signal:

- `_home()` (`agent/config.py`) — most callers
- `default_config().home` — `cli_insights.py`, `cli_task.py`
- `cfg.session.db_path` — anything wired through dependency injection
- `kanban_home()` / `kanban_db_path()` / `boards_root()` — kanban only
- `evolution_home()` (`evolution/storage.py`) — evolution profile-scoped paths
- `Path.home() / ".opencomputer" / ...` — `evolution/rate_limit.py`'s default (the §3.2 gotcha)

Tests should prefer `cfg.session.db_path` (test-injectable) over
`_home()` (calls `os.environ` directly).

### 6.3 Three migration patterns

| Pattern | DBs using it |
|---|---|
| **Numbered Python dict + `schema_version` row** | sessions.db, motifs.sqlite, graph.sqlite, drift_reports.sqlite |
| **Discovered SQL files (`migrations/*.sql`) + `MAX(version)`** | trajectory.sqlite |
| **No version, just `CREATE TABLE IF NOT EXISTS`** | evolution/rate.db, evals/history.db, kanban.db, sessions.db's *attached* tables (`tasks`/`outgoing_messages`/`plugin_demand`/`session_state`) |

The third pattern works because SQLite's `CREATE TABLE IF NOT EXISTS`
is idempotent. It composes safely with the numbered migrations on
`sessions.db` because the attached tables don't share names with any
core table.

### 6.4 Column-name aliases (same concept, multiple names)

| Concept | Names found in production |
|---|---|
| Row creation time | `created_at`, `started_at`, `timestamp`, `enqueued_at`, `ts`, `applied_at` |
| Last modification | `last_updated`, `last_seen_at`, `vibe_updated`, `recall_penalty_updated_at` |
| Result publication | `retrieved_at`, `scored_at`, `granted_at`, `last_heartbeat_at` |

Each name made sense in its local context. The doc records the
divergence; normalization is parked in §8.

### 6.5 Two `tasks` tables — same name, different DB

`sessions.db` has a `tasks` table (`opencomputer/tasks/store.py`) for
**detached LLM tasks** — the queued/running/done/failed/cancelled/orphaned
lifecycle. `kanban.db` *also* has a `tasks` table
(`opencomputer/kanban/db.py`) for **kanban tickets** — the
triage/todo/ready/running/blocked/done/archived lifecycle.

The collision is harmless because:

- They live in **different files** (`sessions.db` vs `kanban.db`).
- They serve **different lifecycles**.
- They're queried by **different modules** (`TaskStore` vs `kanban.db.connect()`).

Renaming either to disambiguate would break compatibility with
existing on-disk DBs. The doc flags the collision; reconciliation is
parked in §8.

### 6.6 Stale `consent/audit.db` reference

`opencomputer/cli_backup.py:197` reads:

```python
consent_db = staged_profile / "consent" / "audit.db"
```

This is **restore-only** — it inspects a previously-staged backup
archive that may carry an older layout. Modern writes go to
`sessions.db.audit_log` (added in schema v3). The path is not
materialized by current code. Keeping the reference protects
backup-restore for users with archives from before consent moved into
the megastore.

`grep -rn "audit\.db" --include="*.py"` returns this single
restore-only site (verified during plan execution).
````

- [ ] **Step 8.2: Verify the conventions audit claims**

Run:

```bash
grep -rEn "audit\.db" /Users/saksham/Vscode/claude/OpenComputer --include="*.py" 2>/dev/null | grep -v __pycache__ | grep -v "\.venv"
```

Expected: only `cli_backup.py:197`. If anything else, fix the doc.

- [ ] **Step 8.3: Commit Task 8**

```bash
git add OpenComputer/docs/databases.md
git commit -m "docs(databases): add §6 conventions audit (the all-over-the-place callout)"
```

---

## Task 9: Write §7 — reading the code (pointer table)

**Files:** Modify `OpenComputer/docs/databases.md` (append §7)

- [ ] **Step 9.1: Append §7 to the doc**

Append:

````markdown

---

## 7. Reading the code

Quick-reference table of where to start for each DB:

| DB | Start at | Key class | Accessor pattern |
|---|---|---|---|
| `sessions.db` | `opencomputer/agent/state.py` | `SessionDB` | `cfg.session.db_path` |
| `evolution/trajectory.sqlite` | `opencomputer/evolution/storage.py` | `TrajectoryStorage` | `trajectory_db_path()` |
| `evolution/rate.db` | `opencomputer/evolution/rate_limit.py` | `DraftRateLimiter` | `DraftRateLimiter().db_path` (default if not pinned) |
| `inference/motifs.sqlite` | `opencomputer/inference/storage.py` | `MotifStore` | `_default_db_path()` |
| `user_model/graph.sqlite` | `opencomputer/user_model/store.py` | `UserModelStore` | `_default_db_path()` |
| `user_model/drift_reports.sqlite` | `opencomputer/user_model/drift_store.py` | `DriftStore` | `_default_db_path()` |
| `kanban.db` | `opencomputer/kanban/db.py` | (top-level functions: `connect`, `init_db`) | `kanban_db_path()` |
| `evals/history.db` | `opencomputer/evals/history.py` | (top-level functions: `record_run`, `load_recent_runs`) | env override or `evals/history.db` rel-cwd |

### Where to add a new table for feature X

A decision tree:

1. **Profile-scoped, session-life data?** → add as a new table inside
   `sessions.db`. If it's a small concern for the agent loop, add a
   migration in `agent/state.py`. If it belongs to a separate
   subsystem (like `tasks`/`outgoing_messages`/`plugin_demand`), add
   a `CREATE TABLE IF NOT EXISTS` in that subsystem's module.
2. **Profile-scoped, large or independent data?** → new sub-DB at
   `<profile_home>/<feature>/<feature>.sqlite`. Use the numbered
   migrations + `schema_version` row pattern (see `inference/storage.py`
   for the canonical template).
3. **Cross-profile coordination?** → add to `kanban.db` (only if it's
   genuinely a coordination primitive). Otherwise, push back: kanban
   is shared by design and accidentally adding orthogonal state there
   couples unrelated subsystems.
4. **Project-local, not user-state?** → new `<repo>/evals/<feature>.db`
   (or similar). Cwd-scoped DBs are rare; only add when "rerun in this
   checkout" is the lifecycle.

The 18-month-old answer to "where do I add Y" should be findable in
this section by reading 30 seconds. If it isn't, refactor this
subsection.
````

- [ ] **Step 9.2: Commit Task 9**

```bash
git add OpenComputer/docs/databases.md
git commit -m "docs(databases): add §7 reading the code (decision tree for new tables)"
```

---

## Task 10: Write §8 — future cleanup candidates (parked)

**Files:** Modify `OpenComputer/docs/databases.md` (append §8)

- [ ] **Step 10.1: Append §8 to the doc**

Append:

````markdown

---

## 8. Future cleanup candidates (parked — explicitly NOT in scope here)

This document records reality. Each item below is a candidate for
future work but is **not** addressed by this doc. Each gets its own
spec and PR if/when prioritized.

| # | Cleanup | Rough effort | Risk |
|---|---|---|---|
| 1 | Normalize `.db` vs `.sqlite` extension across all 8 files. | small (rename + path-helper updates) | low — `WAL`-mode SQLite doesn't care about the suffix; only the disk filenames change. |
| 2 | Unify the six path-resolution helpers behind one canonical helper (likely `cfg.session.db_path`-style for everything). | medium (touches every owner module + test injection sites) | medium — easy to miss a call site and silently route writes to the wrong file. |
| 3 | Unify the three migration patterns. Pick one (likely `agent/state.py`'s numbered Python dict) and migrate the others. The `evolution/storage.py` `# TODO(F1)` comment already plans this. | medium-large | medium — migration code is high-blast-radius; needs a backup-and-rollback playbook. |
| 4 | Fix `evolution/rate.db` to honor `OC_HOME` and the active profile (currently hardcodes `Path.home() / ".opencomputer" / ...`). | small | medium — changes write path; existing rate state at the old path becomes orphaned. Migration step required. |
| 5 | Rename one of the two `tasks` tables to disambiguate (`detached_tasks` in `sessions.db` or `kanban_tasks` in `kanban.db`). | medium | medium — breaks compatibility with on-disk DBs; needs migration. Probably not worth it. |
| 6 | Standardize column names (`created_at` everywhere, instead of the ten current aliases). | medium | low if done table-by-table with `ALTER TABLE RENAME COLUMN`; high if attempted as a single sweep. |
| 7 | Remove the stale `consent/audit.db` reference in `cli_backup.py:197` after enough time has passed that no one has pre-v3 backup archives. | trivial | low. Probably not yet — wait until backup format gets a hard schema bump. |
| 8 | Document each migration in CHANGELOG.md going forward (most are documented inline only). | per-PR ongoing | low. |

When picking up any of these, write a fresh design spec under
`docs/superpowers/specs/`, link back to this doc, and confirm the
"no disruption to on-disk DBs" invariant is upheld for any user
running an existing install.

---

## Companion documents

- **Design spec for this doc:** [`docs/superpowers/specs/2026-05-06-sqlite-organization-design.md`](superpowers/specs/2026-05-06-sqlite-organization-design.md)
- **F4 user-model + Honcho hybrid:** [`docs/memory-architecture.md`](memory-architecture.md)
- **Memory dreaming pipeline:** [`docs/memory_dreaming.md`](memory_dreaming.md)
- **Evolution subsystem design:** [`docs/evolution/design.md`](evolution/design.md)

> **Last verified against source:** 2026-05-06 (commit `git rev-parse HEAD`).
> Re-run the verification greps in the plan
> (`docs/superpowers/plans/2026-05-06-sqlite-organization.md` Tasks
> 2, 4.2, 4.3, 5.2, 5.3, 6.2, 6.3, 7.2, 8.2) when this document feels
> stale.
````

- [ ] **Step 10.2: Commit Task 10**

```bash
git add OpenComputer/docs/databases.md
git commit -m "docs(databases): add §8 parked cleanups + companion-doc index"
```

---

## Task 11: Render check + line cap + spec-cross-check

**Files:** read-only

- [ ] **Step 11.1: Verify line cap (target < 800 lines)**

Run:

```bash
wc -l /Users/saksham/Vscode/claude/OpenComputer/docs/databases.md
```

Expected: < 800. If over, trim least-useful filler (favor tables over prose).

- [ ] **Step 11.2: Render check — confirm Markdown structure parses**

Run:

```bash
grep -cE "^## " /Users/saksham/Vscode/claude/OpenComputer/docs/databases.md
grep -cE "^### " /Users/saksham/Vscode/claude/OpenComputer/docs/databases.md
```

Expected: 8 top-level `## ` sections (TL;DR + 8 numbered) and a healthy count of `### ` subsections (≥ 12). If counts look off, eyeball the file in `less`.

- [ ] **Step 11.3: Confirm internal anchor links resolve**

Each `[N. Title](#N-title)` link must match a heading. Visually verify by running:

```bash
awk '/^## / {gsub(/^## /,""); gsub(/[\.,:]/,""); gsub(/ /,"-"); print tolower($0)}' /Users/saksham/Vscode/claude/OpenComputer/docs/databases.md
```

Expected: outputs anchor names matching the `(#anchor)` references in the ToC (e.g. `tldr`, `1-filesystem-map`, `2-the-megastore-sessionsdb`, …). Fix any mismatches by editing the ToC anchors.

- [ ] **Step 11.4: Run the success-criteria self-test**

Open the doc and read it as if you've never seen the codebase. Verify in under 60 seconds each:

1. "Where does the consent audit log live?" → answer in §2 (`audit_log` row, immutable).
2. "Is `evolution/rate.db` profile-aware?" → answer in §3.2 (no, hardcoded gotcha).
3. "Why are there two `tasks` tables?" → answer in §6.5 (different DBs, different lifecycles).
4. "Where do I add a new feature's table?" → answer in §7 (decision tree).

If any answer takes > 60 seconds, the doc needs reordering.

- [ ] **Step 11.5: No-code-diff check (the load-bearing invariant)**

Run:

```bash
cd /Users/saksham/Vscode/claude
git diff main..HEAD -- '*.py' '*.sql' '*.json' '*.toml' '*.yaml' | wc -l
```

Expected: **0**. If anything appears, **stop** and revert the offending changes — the user's "no add / no delete" constraint was violated.

- [ ] **Step 11.6: Commit any final fixes (if Step 11.1-11.4 forced edits)**

```bash
git status --short
# If only databases.md is modified:
git add OpenComputer/docs/databases.md
git commit -m "docs(databases): final render + line-cap fixes"
```

---

## Task 12: Optional CLAUDE.md pointer (gated on user approval)

**Files:** Modify `OpenComputer/CLAUDE.md`

> **GATE.** Skip this entire task if the user prefers strict "no edits
> to existing files." The doc works without the pointer; it's purely
> a discoverability shortcut.

- [ ] **Step 12.1: Read the current §9 of CLAUDE.md to find the insertion point**

Run:

```bash
grep -n "^## 9\.\|If you need to dig deeper\|^## 10\." /Users/saksham/Vscode/claude/OpenComputer/CLAUDE.md | head -5
```

Note the line where §9 starts (likely around `## 9. If you need to dig deeper`).

- [ ] **Step 12.2: Add a single bullet under §9**

Insert this bullet immediately after the existing "**Per-repo extraction notes**" line in §9 of `OpenComputer/CLAUDE.md`:

```markdown
- **Storage map (every SQLite DB, table, owner module):** `OpenComputer/docs/databases.md` — single canonical reference for `sessions.db` + 7 sub-DBs.
```

(Use the `Edit` tool with `old_string` = the surrounding two existing lines and `new_string` = those same two lines plus the new bullet, to keep the edit surgical.)

- [ ] **Step 12.3: Verify the edit is one bullet only**

Run:

```bash
git diff /Users/saksham/Vscode/claude/OpenComputer/CLAUDE.md | head -20
```

Expected: one `+` line (the new bullet). No other changes.

- [ ] **Step 12.4: Commit Task 12**

```bash
git add OpenComputer/CLAUDE.md
git commit -m "docs(CLAUDE.md): pointer to docs/databases.md in §9"
```

---

## Task 13: Final no-code-diff check + push + (optional) PR

**Files:** read-only — git only

- [ ] **Step 13.1: Confirm the entire branch touches no code**

Run:

```bash
cd /Users/saksham/Vscode/claude
git diff main..HEAD --stat
```

Expected output: only `OpenComputer/docs/databases.md` and (if Task 12 ran) `OpenComputer/CLAUDE.md` and the spec under `OpenComputer/docs/superpowers/specs/`. **No `*.py`, `*.sql`, `*.json`, `*.toml`, `*.yaml`.**

- [ ] **Step 13.2: Run the strict load-bearing invariant**

```bash
git diff main..HEAD -- '*.py' '*.sql' '*.json' '*.toml' '*.yaml'
```

Expected: empty.

- [ ] **Step 13.3: Push to origin and (if requested) open a PR**

Run:

```bash
git push -u origin docs/sqlite-organization-map
```

If the user wants a PR (ask first):

```bash
gh pr create --title "docs: SQLite organization map (no schema changes)" --body "$(cat <<'EOF'
## Summary
- New `OpenComputer/docs/databases.md` — single canonical map of every SQLite DB, table, owner module, and convention quirk.
- Companion design spec at `docs/superpowers/specs/2026-05-06-sqlite-organization-design.md`.
- Optional 1-line pointer in `OpenComputer/CLAUDE.md` (Task 12; included if user approved).

## Constraint upheld
- `git diff main..HEAD -- '*.py' '*.sql' '*.json' '*.toml' '*.yaml'` is **empty**.
- Zero schema changes, zero migration bumps, zero data movement.

## Test plan
- [ ] Read `databases.md` and verify each section answers the success-criteria questions in <60 seconds.
- [ ] Re-run `find . -name "*.db" -o -name "*.sqlite*"` and confirm output matches the doc.
- [ ] Re-run `grep -rn "CREATE TABLE" --include="*.py"` and confirm every match is documented.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 13.4: Mark plan complete**

The plan is done when:

- The doc exists at `OpenComputer/docs/databases.md`.
- It passes all render/line-count/anchor checks (Task 11).
- The no-code-diff invariant holds (Task 13).
- The branch is pushed (Task 13.3).

---

## Self-review — checklist run after writing this plan

- [x] **Spec coverage:** every section of the spec has a corresponding task. §1-9 of the spec map to Tasks 3-10. The optional CLAUDE.md pointer is Task 12 (gated). Risk mitigations from the spec are absorbed into the verification steps in each task.
- [x] **Placeholder scan:** no "TBD", "TODO", "implement later", or "fill in details" anywhere. Every step shows the exact code/text to write.
- [x] **Type consistency:** N/A (this is a doc-only plan; no method signatures to track).
- [x] **No-code-diff invariant** is explicitly checked at Tasks 11.5 and 13.2.
- [x] **Granularity:** each step is one atomic action (write a section, run a verification, commit). 2-5 minutes each.
- [x] **Frequent commits:** one commit per major section (8 commits) plus Task 11 fixup + optional Task 12 pointer + push.
