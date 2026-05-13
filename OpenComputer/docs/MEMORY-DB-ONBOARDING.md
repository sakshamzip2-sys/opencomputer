# Memory + SQLite onboarding

> **What this is.** A 10-minute orientation for anyone new to
> OpenComputer who needs to know "where is X stored, and how does the
> profile-scoping work?" Reading this should leave you confident enough
> to add a new table, debug a missing-row report, or trace a per-profile
> path question without grepping blindly.
>
> **What this is NOT.** The canonical column-by-column reference. For
> that, see [`docs/databases.md`](databases.md) — this doc is the
> entry point; `databases.md` is the depth.

## 1. The two truths to internalize first

1. **The default profile lives at the root, not inside a `default/`
   subdir.** `~/.opencomputer/sessions.db` (no profile segment) IS the
   default profile's megastore. Named profiles get their own subdirs
   one level deeper: `~/.opencomputer/profiles/<name>/sessions.db`.
   This asymmetry is intentional — it means installs predating
   multi-profile support migrate to v0.5+ with zero file moves.

2. **`OPENCOMPUTER_HOME` is the *resolved* active-profile dir, not the
   *root.*** The CLI flag `oc -p <name>` does not export an
   `OC_PROFILE` variable. Instead, `opencomputer/cli.py` calls
   `_apply_profile_override`, which sets
   `OPENCOMPUTER_HOME=~/.opencomputer/profiles/<name>` (or
   `OPENCOMPUTER_HOME=~/.opencomputer` for the default profile) and
   strips the flag from `argv` before Typer dispatch. Code that wants
   the cross-profile root reads `OPENCOMPUTER_HOME_ROOT` (test
   override) or computes it via
   `opencomputer.profiles.get_default_root()`.

Memorize those two and most "where does X live?" questions answer
themselves.

## 2. The path-helper map

| Helper | Module | Returns |
|---|---|---|
| `_home()` | `opencomputer/agent/config.py:42` | The active profile's home directory (`OPENCOMPUTER_HOME` or `~/.opencomputer`). Honors the `current_profile_home` ContextVar (per-asyncio-Task scope, set by the gateway dispatcher). |
| `cfg.session.db_path` | `opencomputer/agent/config.py:471` | `_home() / "sessions.db"`. The canonical, test-injectable accessor for the megastore. Prefer this over inlining `_home() / "sessions.db"` in new code. |
| `get_default_root()` | `opencomputer/profiles.py:67` | The cross-profile root (`~/.opencomputer` or `$OPENCOMPUTER_HOME_ROOT`). Use this when you need the parent that holds `profiles/<name>/` subdirs. |
| `get_profile_dir(name)` | `opencomputer/profiles.py:80` | Resolves a profile name to its home dir. `None`/`"default"` → root; named → `<root>/profiles/<name>/`. |
| `kanban_home()` | `opencomputer/kanban/db.py:85` | The cross-profile root, with a walk-up if `_home()` is under `profiles/<name>/`. Kanban is shared across profiles by design — see §4. |
| `evolution_home()` | `opencomputer/evolution/storage.py:33` | `_home() / "evolution"` — profile-scoped evolution dir. Distinct from the `DraftRateLimiter` default constructor (see §5). |

## 3. The 8-file owned inventory + 1 optional

Path formulas assume `<profile_home>` is whichever home `_home()`
returns for the active profile (root for default, `<root>/profiles/<name>`
for named).

| # | File | Path | Owner module | Profile-scoped? |
|---|---|---|---|---|
| 1 | `sessions.db` | `<profile_home>/sessions.db` | `opencomputer/agent/state.py` | yes |
| 2 | `evolution/trajectory.sqlite` | `<profile_home>/evolution/trajectory.sqlite` | `opencomputer/evolution/storage.py` | yes |
| 3 | `evolution/rate.db` | depends on call site (see §5) | `opencomputer/evolution/rate_limit.py` | call-site-dependent |
| 4 | `inference/motifs.sqlite` | `<profile_home>/inference/motifs.sqlite` | `opencomputer/inference/storage.py` | yes |
| 5 | `user_model/graph.sqlite` | `<profile_home>/user_model/graph.sqlite` | `opencomputer/user_model/store.py` | yes |
| 6 | `user_model/drift_reports.sqlite` | `<profile_home>/user_model/drift_reports.sqlite` | `opencomputer/user_model/drift_store.py` | yes |
| 7 | `kanban.db` | `<oc_root>/kanban.db` | `opencomputer/kanban/db.py` | **shared across profiles** |
| 8 | `evals/history.db` | `$CWD/evals/history.db` (or `$OPENCOMPUTER_EVAL_HISTORY_DB`) | `opencomputer/evals/history.py` | cwd-scoped (project-local) |
| opt | `memory-vector/chroma.db` | `<profile_home>/memory-vector/chroma.db` | `extensions/memory-vector/backend.py` | yes (only with plugin) |

## 4. The megastore (`sessions.db`)

- **Schema version** lives at `SCHEMA_VERSION` in
  `opencomputer/agent/state.py:137` (currently **19**). One schema
  version covers all table additions made within
  `apply_migrations()`.
- **Migration style:** numbered Python dict
  `MIGRATIONS: dict[tuple[int, int], str]` plus a single-row
  `schema_version` table. Each migration runs in its own transaction.
- **Owner-declared tables (16 + 2 FTS5):** `schema_version`,
  `sessions`, `messages`, `messages_fts` (FTS5), `episodic_events`,
  `episodic_fts` (FTS5), `consent_grants`, `consent_counters`,
  `audit_log`, `prompt_checkpoints`, `tool_usage`, `vibe_log`,
  `turn_outcomes`, `recall_citations`, `policy_changes`,
  `policy_audit_log`, `llm_calls`, `subagents`.
- **Attached tables (4, `CREATE TABLE IF NOT EXISTS`, no schema
  coordination):** `tasks` (from `tasks/store.py`),
  `outgoing_messages` (`gateway/outgoing_queue.py`), `plugin_demand`
  (`plugins/demand_tracker.py`), `session_state` (from
  `extensions/coding-harness/tools/todo_write.py`).
- **Concurrency:** WAL mode + application retry-with-jitter on
  `SQLITE_BUSY`. WAL means the on-disk file family is
  `sessions.db` + `sessions.db-wal` + `sessions.db-shm`. **Do not let
  cloud-sync tools touch any of these three** (see §7).

A fresh `sessions.db` contains many extra rows in `sqlite_master`
beyond those 16 + 2 + 4 — FTS5 builds three or four shadow tables per
virtual table (`messages_fts_data`, `messages_fts_idx`,
`messages_fts_content`, `messages_fts_docsize`, `messages_fts_config`),
plus triggers and indexes. None of those are user-visible — count
"tables" by the schema definitions, not by raw `sqlite_master` rows.

## 5. The rate-limiter gotcha (`evolution/rate.db`)

`DraftRateLimiter` is constructed two different ways with two
different paths:

- **Default constructor** at `opencomputer/evolution/rate_limit.py:37`:
  if `db_path` is omitted, the limiter writes to
  `~/.opencomputer/evolution/rate.db` regardless of
  `OPENCOMPUTER_HOME` or the active profile. **Direct callers (tests,
  ad-hoc scripts) hit this path.**

- **Production** at
  `opencomputer/evolution/procedural_memory_loop.py:86`: the
  `ProceduralMemoryLoop` always passes an explicit
  `<profile_home>/evolution/rate.db`. The agent in production
  therefore writes per-profile rate counters correctly.

So the rate.db is *correctly* per-profile in the running agent. The
gotcha only fires for direct `DraftRateLimiter()` constructions —
those leak to the home-dir-rooted default. Fixing the default
constructor is parked in `databases.md` §8.

## 6. The kanban walk-up (`kanban_home()`)

`kanban.db` is **deliberately shared across profiles** — a worker
spawned with `oc -p worker chat` must join the same board as whoever
dispatched the task. The walk-up in `kanban_home()` is what makes that
work:

- Default profile: `_home() = ~/.opencomputer`, parent is `~`, not
  `profiles` → returns `~/.opencomputer` directly. Kanban lives at
  `~/.opencomputer/kanban.db`. ✓
- Named profile: `_home() = ~/.opencomputer/profiles/work`, parent is
  `profiles` → walks up two levels to `~/.opencomputer`. Kanban lives
  at `~/.opencomputer/kanban.db`, **same file as the default profile**.
  ✓
- Docker default: `OPENCOMPUTER_HOME=/opt/oc`, parent is `/opt`, not
  `profiles` → returns `/opt/oc`. Kanban at `/opt/oc/kanban.db`. ✓
- Docker named: `OPENCOMPUTER_HOME=/opt/oc/profiles/worker`, walks up
  → `/opt/oc/kanban.db`. ✓

Three env-var overrides bypass the walk-up entirely (highest
precedence first): `OC_KANBAN_DB` (pin DB file), `OC_KANBAN_HOME` (pin
root), `OC_KANBAN_BOARD` (slug → per-board path under
`<root>/kanban/boards/<slug>/`).

## 7. WAL + cloud-sync = corruption

SQLite's write-ahead-log mode (WAL) splits a logical write across
three on-disk files: `<db>`, `<db>-wal`, and `<db>-shm`. Dropbox,
iCloud Drive, OneDrive, and Syncthing do not atomically sync the three
files — a partial sync exposes the next reader to a `-wal` that
disagrees with the main file, corrupting the DB.

**Action.** If `~/.opencomputer/` is inside a cloud-synced folder
(common when a user puts `~/Documents/` or `~/Sync/` in their home),
add **at minimum** these patterns to the sync tool's ignore list:

```
~/.opencomputer/sessions.db
~/.opencomputer/sessions.db-wal
~/.opencomputer/sessions.db-shm
~/.opencomputer/profiles/*/sessions.db
~/.opencomputer/profiles/*/sessions.db-wal
~/.opencomputer/profiles/*/sessions.db-shm
~/.opencomputer/kanban.db
~/.opencomputer/kanban.db-wal
~/.opencomputer/kanban.db-shm
```

(`MEMORY.md`, `USER.md`, `DREAMS.md`, `SOUL.md`, and `config.yaml` ARE
safe to sync — they're plain text/YAML.) The simplest correct fix is
to exclude `~/.opencomputer/` entirely from the cloud-sync tool and
move only the markdown files into a synced folder.

README.md §"Cloud-synced `~/.opencomputer/`" carries the
authoritative one-paragraph warning; this section is the
operationally-explicit version.

## 8. External SQLite files we touch but don't own

| DB | Path | Used by | Mode |
|---|---|---|---|
| macOS Messages `chat.db` | `~/Library/Messages/chat.db` (override: `IMESSAGE_DB_PATH`) | `opencomputer/skills/profile_scraper/scraper.py` | read-only (tempfile copy) |
| Chromium-family `History` | `~/Library/Application Support/{Google/Chrome, BraveSoftware/Brave-Browser, Microsoft Edge, Vivaldi, Arc/User Data, Chromium}/<Profile>/History` | `opencomputer/profile_bootstrap/browser_history.py` | read-only (tempfile copy) |
| ChromaDB `chroma.db` + `chroma.sqlite3` | `<profile_home>/memory-vector/chroma.db` | `extensions/memory-vector/backend.py` | read/write (only when plugin loaded) |

The Messages and browser-history readers are macOS-only — they
silently no-op on Linux/Windows. Both use `shutil.copyfile` to grab a
tempfile snapshot *before* opening with `sqlite3.connect`, because
running browsers hold an exclusive lock on their live `History`.

## 9. Where to add a new table

A 3-question decision tree:

1. **Profile-scoped + small + agent-loop-adjacent?** → add a table to
   `sessions.db`. Bump `SCHEMA_VERSION` in `agent/state.py` and add a
   numbered `_migrate_vN_to_vN+1` function.
2. **Profile-scoped + large or independent subsystem?** → new sub-DB
   at `<profile_home>/<feature>/<feature>.sqlite`. Use the numbered
   Python-dict migration pattern (see `inference/storage.py` for the
   canonical template).
3. **Cross-profile coordination?** → add to `kanban.db` only if the
   data is genuinely a coordination primitive. Otherwise push back —
   accidentally adding orthogonal state to kanban couples unrelated
   subsystems.

If you're tempted to introduce a fourth on-disk file because none of
the three above fit, write a one-paragraph rationale and propose it in
`docs/superpowers/specs/`. The 9th SQLite file is a lossy abstraction
boundary — make the case explicitly.

## 10. Companion docs

- **Canonical schema reference:** [`docs/databases.md`](databases.md)
- **Memory architecture (F4 user-model + Honcho hybrid):** [`docs/memory-architecture.md`](memory-architecture.md)
- **Memory dreaming pipeline:** [`docs/memory_dreaming.md`](memory_dreaming.md)
- **Evolution subsystem design:** [`docs/evolution/design.md`](evolution/design.md)
- **Design spec (rationale):** [`docs/superpowers/specs/2026-05-06-sqlite-organization-design.md`](superpowers/specs/2026-05-06-sqlite-organization-design.md)

> **Last verified against source:** 2026-05-12 — confirmed
> `SCHEMA_VERSION=19`, env vars (`OPENCOMPUTER_HOME` /
> `OPENCOMPUTER_HOME_ROOT`), profile path scheme, kanban walk-up,
> rate.db call-site split, Chromium browser History + ChromaDB
> external coverage.
