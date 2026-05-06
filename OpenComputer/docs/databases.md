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
