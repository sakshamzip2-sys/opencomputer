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
| Evolution | `evolution/rate.db` | `~/.opencomputer/evolution/rate.db` | **no** (gotcha — see §3.2) |
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
