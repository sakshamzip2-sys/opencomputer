# SQLite Organization — single canonical map (no schema changes)

> **Honest framing.** Saksham looked at OpenComputer's SQLite and felt
> it was "all over the place." He explicitly said: *do not add anything,
> do not delete anything — just structure it properly so Claude Code can
> understand exactly what's there.* This spec is the answer: a
> documentation-only deliverable that catalogs every existing DB without
> moving a byte of data.

## Why this exists

OpenComputer ships **8 distinct SQLite files** today (4 use `.db`, 4
use `.sqlite`), plus a "megastore" (`sessions.db`) that **5 different
modules write into**. The megastore holds 19 SQLite objects: 13 base
tables and 2 FTS5 virtual tables declared in `agent/state.py`, plus 4
tables contributed by `tasks/store.py`, `gateway/outgoing_queue.py`,
`plugins/demand_tracker.py`, and
`extensions/coding-harness/tools/todo_write.py`. There is no single
place that tells a reader:

- which file lives where,
- which module owns it,
- what tables are inside,
- how migrations work for that DB,
- what's profile-scoped vs cross-profile vs cwd-scoped.

That gap is the source of the "all over the place" feeling. Three things
in particular create the impression of disorder:

1. **Two file extensions silently coexist for the same job** — `.db`
   for `sessions`/`kanban`/`evals`/`rate`/`audit`; `.sqlite` for
   `trajectory`/`motifs`/`graph`/`drift_reports`. No principle.
2. **Six path-resolution helpers**, each module rolled its own:
   `_home()`, `default_config().home`, `cfg.session.db_path`,
   `kanban_home()`, `evolution_home()`, plus a hardcoded
   `Path.home() / ".opencomputer" / ...` fallback in `evolution/rate_limit.py`.
3. **Three migration patterns coexist**: numbered Python dict +
   `schema_version` row (state, motifs, graph, drift), discovered SQL
   files (evolution), and "just `CREATE TABLE IF NOT EXISTS`" (eval
   history, outgoing queue, demand tracker, kanban, todo_write, drafts).

None of those are *wrong*. They just aren't documented in one place.

## Goal

Produce **one canonical document** at
`OpenComputer/docs/databases.md` that maps the entire SQLite landscape:
every file, every table, every owner, every quirk. That doc becomes the
single load-bearing reference a Claude Code session (or a human) reads
to answer "where is X stored, who owns it, how does it migrate?"

After this work, anyone asking "where is consent stored?" or "is rate.db
profile-scoped?" or "why does kanban have a `tasks` table when there's
already one in `sessions.db`?" finds the answer in one place.

## Hard constraints (non-goals)

These are **off-limits** for this work:

- ❌ **No new database files.** Don't create another `.db` or `.sqlite`.
- ❌ **No table changes.** No `CREATE`, no `DROP`, no `ALTER`, no rename.
- ❌ **No schema-version bumps** in any of the 4 numbered-migration DBs.
- ❌ **No code refactors.** Don't unify the path helpers, don't normalize
  `.db` vs `.sqlite`, don't merge `tasks` tables.
- ❌ **No deletions** — even the stale `consent/audit.db` reference in
  `cli_backup.py` (it's a legacy restore-archive path; document it,
  don't remove it).
- ❌ **No data migration.** No moving rows between DBs.

The only writes are to **new markdown files** plus a *minimal* pointer
edit (1–2 lines) in `OpenComputer/CLAUDE.md` so future sessions discover
the new doc on auto-load. The pointer edit is optional and explicitly
flagged.

After this lands, `git diff -- '*.py' '*.sql'` must show **zero changes**.

## Deliverable

A single document plus one optional pointer:

### Required: `OpenComputer/docs/databases.md`

A long-form, well-organized reference. The structure (sized to its content):

1. **TL;DR** — 5-line overview. "8 SQLite files. One per-profile
   megastore (`sessions.db`) holds 18 tables across 5 owner modules.
   Five sub-DBs sit beside it under `<profile_home>/`. Two DBs sit
   *outside* the profile by design (`kanban.db` cross-profile;
   `evals/history.db` cwd-scoped)."

2. **The map** — ASCII filesystem tree showing exactly where each DB
   lives under `~/.opencomputer/<profile>/...` and where the
   non-profile DBs live (`<root>/kanban.db`, `OPENCOMPUTER_EVAL_HISTORY_DB`).

3. **The megastore: `sessions.db`** — table-by-table catalog of all
   19 SQLite objects (13 base tables + 2 FTS5 virtual tables in
   `agent/state.py`, plus 4 tables attached by `tasks/store.py`,
   `gateway/outgoing_queue.py`, `plugins/demand_tracker.py`, and
   `extensions/coding-harness/tools/todo_write.py`). For each: owning
   module, purpose, schema-version it landed in (where applicable),
   primary-key shape, key indexes. Includes the immutable-by-trigger
   `audit_log` and the two FTS5 shadow indexes (`messages_fts`,
   `episodic_fts`) with their auto-sync triggers.

4. **Profile-scoped sub-DBs** (5 files) — one section each:
   `evolution/trajectory.sqlite`,
   `evolution/rate.db`,
   `inference/motifs.sqlite`,
   `user_model/graph.sqlite`,
   `user_model/drift_reports.sqlite`.
   For each: path formula, owner module, tables, migration style,
   schema version. Calls out that `rate.db` is the **one accidentally
   non-profile-aware** DB (hardcodes `Path.home() / ".opencomputer"`)
   and explicitly leaves the behavior unchanged.

5. **Cross-profile / non-profile DBs** (2 files):
   - `kanban.db` — cross-profile by **design** (it IS the
     coordination primitive). Documents the multi-board layout,
     the three env overrides (`OC_KANBAN_DB`, `OC_KANBAN_BOARD`,
     `OC_KANBAN_HOME`, `OC_KANBAN_WORKSPACES_ROOT`), and the 11
     tables.
   - `evals/history.db` — cwd-scoped, not profile-scoped, overridable
     via `OPENCOMPUTER_EVAL_HISTORY_DB`. Single `eval_runs` table.

6. **External DBs touched but not owned** —
   `~/Library/Messages/chat.db` (macOS iMessage scraper, read-only),
   plus the `IMESSAGE_DB_PATH` override. Documents that these are not
   ours; we just read them.

7. **Conventions audit (the "all over the place" callout)** —
   spells out the four observed inconsistencies so they're visible,
   without "fixing" them:
   - `.db` vs `.sqlite` extension drift
   - Six path-resolution helpers (with the call sites listed)
   - Three migration patterns (which DBs use which)
   - Column-name aliases (`created_at`, `started_at`, `timestamp`,
     `enqueued_at`, `ts`, `last_updated`, `last_seen_at`, …)
   - Two `tasks` tables (one in `sessions.db`, one in `kanban.db`) —
     same name, different schema, different DB, intentional.
   - Stale `consent/audit.db` reference in `cli_backup.py`
     (restore-archive legacy path; modern writes go to
     `sessions.db.audit_log`).

8. **Reading the code** — pointer table mapping each DB to the
   canonical Python module to start at, plus the recommended
   accessor pattern (`cfg.session.db_path` for the megastore, the
   per-module `_default_db_path()` helpers for the sub-DBs).

9. **Future cleanup candidates (parked)** — explicitly listed but
   marked **not in scope** for this work: extension normalization,
   path-helper unification, migration-pattern unification, naming
   alignment. Each cleanup gets one bullet so a future maintainer
   knows what's been considered and parked.

### Optional: 1-line pointer in `OpenComputer/CLAUDE.md`

If approved, add one line under §9 ("If you need to dig deeper") or §2
(repository layout) of the existing `OpenComputer/CLAUDE.md`:

> **Storage map:** `OpenComputer/docs/databases.md` is the single
> canonical reference for every SQLite file, table, and migration.

This is borderline under the "no add" constraint (it's a 1-line addition
to existing docs, not new code/data). It's marked optional — the design
works without it; the pointer just shortens the discovery path.

## Architecture: how the document is organized for Claude Code's reading model

The document's section ordering is deliberate. A new Claude Code session
arriving with a question about storage typically asks one of three
shapes of question:

- **"Where is X stored?"** — answered by §2 (the map) + the section
  index at the top.
- **"What's in `sessions.db`?"** — answered by §3, the table catalog.
- **"Why is the layout like this?"** — answered by §7 (conventions
  audit) and §9 (parked cleanup).

Each section is **self-contained**: a reader can jump to §4 without
having loaded §3 first. Cross-references are explicit links, not
"see above."

The TL;DR is the only required-read section. Everything else is
random-access.

## What the document is NOT

- It is **not** a schema dump. We don't list every column in every
  table — `git grep "CREATE TABLE"` is the source of truth for that;
  duplicating columns into Markdown invites rot. We list table names,
  primary keys, and the *purpose* of each table, plus the indexes
  that matter for read-side reasoning.
- It is **not** a migration history. The numbered comments in
  `agent/state.py` (`v3 = F1 consent layer tables…`) already do
  that. We summarize where to look, not what every migration did.
- It is **not** a refactor proposal. §9 lists parked cleanups but
  treats them as future work, not a recommendation to act now.
- It is **not** an API doc for the `SessionDB` / `MotifStore` /
  `UserModelStore` Python classes. Those have their own docstrings.
  This doc is about the *files on disk*, not the wrappers around them.

## Success criteria

- A Claude Code session loaded with this doc can answer all of:
  - "Where does the consent audit log live?"
    → `sessions.db.audit_log`, append-only, HMAC-chained.
  - "Is `evolution/rate.db` profile-aware?"
    → No; documented gotcha. Stays that way for this work.
  - "Why are there two `tasks` tables?"
    → One in `sessions.db` (detached LLM tasks), one in `kanban.db`
       (multi-profile coordination). Different schemas, intentional.
  - "Where do I add a new table for feature X?"
    → Decision tree: profile-scoped session-life data → `sessions.db`;
       large/independent → new sub-DB under `<profile_home>/<feature>/`;
       cross-profile coord → `kanban.db`.
- `git diff -- '*.py' '*.sql' '*.json' '*.toml'` after the work is **empty**.
- The new doc is rendered correctly (Markdown lints clean).
- The doc is < 800 lines (tight, scannable, no padding).

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Doc rots when schemas change. | We document **structure**, not column-by-column schemas. The table catalog references file:line so readers `git grep` to verify. Migration history lives in code comments, not here. |
| Doc claims contradict code. | Spec self-review (placeholder / consistency / scope / ambiguity) runs before user review. Implementation plan re-verifies every claim with file reads. |
| Doc grows unwieldy. | 800-line cap. Anything bigger gets split into per-DB pages and indexed from a top-level. |
| User wants more (path-helper unification, extension normalization, etc.). | §9 (parked cleanup) lists those explicitly, gated behind "future work, separate spec." |
| `cli_backup.py`'s stale `consent/audit.db` is *actually* still written somewhere we missed. | Implementation plan grep step verifies: `grep -rn "audit.db" --include='*.py'` shows only `cli_backup.py:197`, which is restore-only. Document that explicitly. |
| **Test fixtures show CREATE TABLE** for `sessions` / `messages` / `urls` etc. — readers might confuse them for production tables. | The doc explicitly distinguishes **production-owned tables** from **test fixtures** (`tests/test_*.py` constructions are NOT canonical schemas — they reproduce minimal subsets to satisfy a test). The doc only catalogs production owners. |
| Schema in production may have moved past `SCHEMA_VERSION = 12` by the time the doc renders. | The doc refers to migration *files* and *line numbers*, not version numbers as the canonical source. Statement: "current schema version: see `SCHEMA_VERSION` constant at `opencomputer/agent/state.py:37`." |
| Reader on Windows / Linux assumes `~/Library/Messages/chat.db` exists. | The "External DBs" section explicitly tags `chat.db` as macOS-only and notes the read-only nature of `IMESSAGE_DB_PATH`. |
| User ignores the optional `CLAUDE.md` pointer and the doc gets orphaned. | Doc filename `databases.md` is searchable verbatim; we also add it to the doc index implicitly by living next to `memory-architecture.md` (the existing reader's first stop for storage questions). |

## Self-audit — assumptions and edge cases stress-tested

Before approving this spec, I tested it against the following:

- **"What if the user actually wanted a code refactor?"** Re-read: he
  said "do not add anything do not delete anything just structure it
  properly." Code refactor *does* add (helpers, registry modules) or
  delete (legacy paths). Documentation does neither. Doc-only is the
  letter and spirit of the constraint.
- **"Is creating a new markdown file 'adding'?"** Yes literally, but
  the user's stated end goal is "Cloud Code will be able to understand
  exactly what I'm talking about." A documentation map is the *means*
  by which that goal is reached. No data is added, no schema is added,
  no DB file is added. The constraint applies to the data layer.
- **"What if there's an undocumented DB I missed?"** The implementation
  plan runs an exhaustive `find . -name "*.db" -o -name "*.sqlite*"`
  AND `grep -rn "CREATE TABLE\|sqlite3.connect" --include="*.py"`
  before writing each section. Any new file/owner found gets cataloged.
- **"What if the doc itself becomes stale?"** Documented in Risks. Doc
  references file:line, not column lists. Migration version refers to
  the constant, not a hardcoded number.
- **"Should the optional `CLAUDE.md` pointer happen?"** We defer to
  the user. The spec works without it. The doc is discoverable via
  `find docs/ -iname "database*"` regardless.
- **"What about the `experiments/textual_prototype/` directory?"**
  Verified empty of SQLite usage. `audit/` directory is markdown-only.
  Both excluded.
- **"What about `setup_wizard.py`'s `IMESSAGE_DB_PATH`?"** External
  reference to macOS Messages — already covered in §6 (External DBs).
- **"Is the `extensions/coding-harness/tools/todo_write.py session_state`
  table really the only extension contributing to sessions.db?"**
  Verified by `grep -rEn "CREATE TABLE" extensions/`. Yes, only one
  match. Other extensions (telegram, discord, providers, dev-tools,
  memory-honcho, skill-evolution) don't touch SQLite directly — they
  use `cfg.session.db_path` indirectly via the SDK.

The spec survives all of these. Refinement complete.

## Non-obvious infra notes (added to ground the doc)

- **`sessions.db` is a megastore by deliberate choice.** `tasks/store.py`
  documents the rationale: "Same DB as sessions — keeps everything in
  one file per profile so `opencomputer profile delete` cleans up
  cleanly. No second DB to worry about." That's the thread tying
  together why so many tables co-tenant: deletion-cleanup is a
  load-bearing constraint.
- **`kanban.db` lives outside `_home()` on purpose.** Module docstring
  says it must NOT be profile-scoped because it IS the cross-profile
  coordination primitive — a worker spawned with `oc -p other-profile`
  must join the same board as the dispatcher. The existing
  `kanban_home()` helper resolves to `_oc_home()` (one level up) by
  design. Calling that out in the doc prevents anyone from "fixing" it
  to `_home() / "kanban.db"` and breaking dispatcher/worker handoff.
- **`evals/history.db` cwd-scoping is also a design choice.** Eval runs
  are reproduced at the *project* level (you run evals from a checked-out
  repo), not at the *user* level. The cwd-scoping makes "rerun this
  eval" deterministic across machines.
- **The `extensions/coding-harness/tools/todo_write.py` `session_state`
  table writes into `sessions.db` via `api.session_db_path`** (not its
  own DB). This is the only extension that adds a table to the
  megastore. It's keyed `(session_id, key)` and used by `TodoWrite`.

## Implementation outline (handoff to writing-plans)

The implementation plan turns this spec into ordered steps:

1. Create `OpenComputer/docs/databases.md` from this spec's outline.
   Each section's content is verified directly from the source: read
   each owner module, copy its DDL summary, list its readers/writers
   from the catalog already produced in the brainstorm.
2. Verify every claim with `git grep` (no fabricated paths, no
   fabricated table names, no fabricated migration numbers).
3. Render check: open the doc, scan headings, confirm all internal
   links resolve, confirm the ASCII tree is monospace-aligned.
4. Run the success-criteria questions against the doc — does it
   answer each one in <60 seconds of reading?
5. Cap-check: confirm < 800 lines, no padding.
6. Ask user about the optional `CLAUDE.md` pointer line.
7. Commit on a dedicated branch; run the no-code-diff check
   (`git diff main..HEAD -- '*.py' '*.sql' '*.json' '*.toml'`) and
   confirm empty.

## Decision log

- **Approach A (single canonical doc) chosen** over Approach B
  (per-module READMEs) and Approach C (diagram-only). A is the highest
  information-density single-source-of-truth. B fragments the picture
  Claude Code wants in one buffer. C loses table-level detail.
- **Spec lives at `OpenComputer/docs/superpowers/specs/`** — sibling of
  the 60+ existing design docs, dated `2026-05-06`.
- **Deliverable lives at `OpenComputer/docs/databases.md`** — sibling
  of `memory-architecture.md` (which already covers F4 specifically).
  Top-level `docs/` placement maximizes discoverability.
- **No code edits** beyond the optional 1-line pointer in `CLAUDE.md`.
  The user's "do not add" constraint is interpreted strictly: no new
  code modules, no helper scripts, no path-resolver utilities. Pure
  documentation.
