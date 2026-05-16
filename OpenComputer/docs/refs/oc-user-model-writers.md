# OC user-model graph — writer audit (T1.2)

Date: 2026-05-16 · Produced by: awareness-cleanup T1.2 · Companion: `oc-user-model-baseline.md`

**Purpose:** enumerate every writer of user-model nodes, classify what each writes, and
quantify how dirty the live graph is — the input that sizes Milestone 2.

---

## Live-graph snapshot (this machine, `~/.opencomputer/user_model/graph.sqlite`)

| Metric | Value |
|---|---|
| Nodes | **179** (preference 131, attribute 41, identity 4, goal 3) |
| Edges | **393,157** (asserts 188,269 · derives_from 204,888) |
| DB file size | **194 MB** |
| `preference` value shape | 131/131 = `"prefers <weekday> <HH>:00 for X"` (100% motif-derived) |
| `attribute` value shape | 24 `<key>: <value>` (bootstrap), 17 `uses X` / `runs X` (motif-derived) |

Two defects are visible without any further analysis: **(a)** every preference node is
a temporal-motif row, many over agent-internal labels (`agent_loop`, `Bash`); **(b)**
393 K edges for 179 nodes — see §4.

---

## 1. Writer inventory

| Writer | File | Kinds written | Provenance |
|---|---|---|---|
| `write_identity_to_graph` | `profile_bootstrap/persistence.py:52` | `identity` | Layer-0 identity reflex |
| `write_interview_answers_to_graph` | `…/persistence.py:88` | `goal`, `preference`, `attribute` | Layer-1 quick interview (user-explicit) |
| `write_recent_files_to_graph` | `…/persistence.py:155` | `attribute` | Layer-2 recent-file scan |
| `write_git_log_to_graph` | `…/persistence.py:192` | `attribute`, `identity` | Layer-2 git-log scan |
| `write_browser_history_to_graph` | `…/persistence.py:255` | `attribute` | Layer-2 browser history |
| `write_calendar_to_graph` | `…/persistence.py:295` | `attribute` | Layer-2 calendar |
| `MotifImporter._import_temporal` | `user_model/importer.py:118` | `attribute`, `preference` | 3.B temporal motif |
| `MotifImporter._import_transition` | `…/importer.py:154` | `attribute` ×2 | 3.B transition motif |
| `MotifImporter._import_implicit_goal` | `…/importer.py:196` | `goal`, `attribute` ×≤3 | 3.B implicit-goal motif |
| `F4HonchoBridge.synthesize_and_materialize` | `user_model/honcho_bridge.py:143` | `preference` | Honcho synthesis (conf ≤0.5) |
| `oc user-model nodes add` | `cli_user_model.py:179` | any | manual CLI |

The motif importer runs **every 5-minute cron tick** via
`cron/system_jobs.py::_run_motif_import_tick` → `import_recent(limit=100)`.

## 2. Value formats by kind

- `identity` — `"name: X"`, `"email: X"`, `"phone: X"`, `"github: X"`, `"city: X"`,
  `"git_author_email: X"`. Format: `"<dimension>: <value>"`. confidence 0.95–1.0.
- `goal` — `"current_focus: X"` / `"current_concerns: X"` (interview) **or**
  `"session goal: X-led (N tools)"` (motif). **Two incompatible formats.**
- `preference` — `"tone_preference: X"` / `"do_not: X"` (interview) **or**
  `"prefers <weekday> <HH>:00 for X"` (motif). **Two incompatible formats.**
- `attribute` — `"<key>: <value>"` (bootstrap: `active_dir:`, `frequent_domain:`,
  `works_on_repo:`, `upcoming:`, `context:`) **or** `"uses X"` / `"runs X"` (motif).

## 3. Mis-classification finding — the honest read

**No writer is mis-classified in the strict sense.** `persistence.py` maps interview
answers sensibly; `importer.py` maps each motif kind to a defensible NodeKind. PART-1's
hypothesis ("a writer dumps cron rows in as preferences") is **not** what's happening.

What *is* happening: the importer faithfully imports motifs that **should never have
been motifs**. Live examples — `attribute: "uses ambient-sensors"`, `"uses cron"`,
`"uses agent_loop"`, `"runs turn_start/agent_loop"`, `"runs session_end/agent_loop"`,
`"runs turn_completed/gateway.dispatch"`, `preference: "prefers Wednesday 20:00 for
agent_loop"`. These labels are the **agent's own event lifecycle**, not user behavior.

The defect is upstream of every node writer: the behavioral-inference engine
(`opencomputer/inference/engine.py`) counts agent-internal event types (`turn_start`,
`tool_call`, `session_end`, `foreground_app`, `gateway.dispatch`) and internal tool
names (`agent_loop`, `ambient-sensors`, `cron`) as user behavioral motifs.

**Implication for M2 (revises PART-2 T2.1):** the plan's example rule — "`preference`
values must match `<dimension>: <value>`" — is **wrong**: the importer's legitimate
preference value is `"prefers <weekday> <time> for X"`, which does not fit that pattern.
The validator must instead be a **value-content denylist**: reject (or `needs_review`-flag)
nodes whose value embeds a known agent-internal token. Recommended denylist seed:
`agent_loop`, `ambient-sensors`, `cron`, `gateway.dispatch`, and the event-type prefixes
`tool_call/`, `turn_start/`, `turn_completed/`, `session_end/`, `session_start/`,
`foreground_app/`. This is implementable and finite.

The deeper fix (engine stops minting these motifs) is **out of awareness-cleanup scope** —
flagged as an Open Question, severity MEDIUM. The validator + migration handle the
*symptom* in the graph; the engine fix is a separate change.

## 4. Edge explosion — DISCOVERED ISSUE, severity HIGH

`MotifImporter` calls `upsert_node` (dedupes by `(kind, value)`) but `insert_edge`
**always inserts a fresh-UUID edge**. `import_recent(limit=100)` runs every cron tick
(~5 min). Result: ~100–300 new edges per tick, forever. The importer docstring claims
duplicates are "harmless until Phase 3.D's drift pass folds them" — but **nothing folds
them**: the drift pass does KL over motifs, not edge dedup, and the decay scheduler that
would at least decay them is dormant (see baseline §5).

**Measured:** 393,157 edges for 179 nodes; 194 MB DB. Unbounded — grows every 5 minutes.

Not a crash (SQLite + indexes hold), but it bloats the DB and slows any full edge scan
(`DecayEngine.apply_decay` walks all edges). **In scope for M2** — this is "bleeding at
source." Recommended M2 task addition: make `_import_*` edge-idempotent (dedupe edges by
`(kind, from_node, to_node, source)` via an `INSERT OR IGNORE`-style upsert, or a
deterministic `edge_id` hash) + a one-time migration to collapse existing duplicates.

## 5. M2 scope + exit criteria (revised from PART-2)

| PART-2 task | Revision after T1.2 |
|---|---|
| T2.1 validator | Rule shape changes: **value-content denylist** of agent-internal tokens, not a `<dimension>: <value>` regex. Per-kind format checks still apply to `identity` (`<dim>: <val>` holds there). |
| T2.3 "walk every offender" | Offender count is **low** (3 writer modules) — but add an **edge-dedup** task (§4). M2 stays M-L; the edge fix is ~1 day. |
| T2.4 migration | Must also collapse 393 K → ~few-hundred edges (dedup migration). Reclassify motif-noise nodes to `needs_review`, don't delete. |

**Exit criterion:** validator rejects/flags agent-internal-token nodes; importer is
edge-idempotent; migration collapses the edge table and flags noise nodes. Accept the
upstream engine fix as deferred (Open Question).

Audit phase (T1.1 + T1.2) complete. Proceed to T1.3 — `oc awareness review`.
