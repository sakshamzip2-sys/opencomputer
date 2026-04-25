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

## Reserved files — Session C only (Session A + B must NOT modify)

- `extensions/opencli-scraper/*`
- `extensions/oi-capability/*` (provisional — Session A's Phase 5 refactors into `extensions/coding-harness/oi_bridge/` per `docs/f7/interweaving-plan.md`)
- `tests/test_opencli_*.py`, `tests/test_oi_*.py`
- `docs/f6/*`, `docs/f7/*`

## Shared files (coordinate)

- `CLAUDE.md` — **Session A only** during scheduled refreshes. Session B flags changes needed via a PR to docs/evolution/README.md; Session A folds them in at next refresh.
- `CHANGELOG.md` — **both sessions append** under `## [Unreleased]`. Merge conflicts on concurrent writes are trivial (rebase + re-append).
- `pyproject.toml` — **both sessions** may add dependencies. Declare in PR description. Resolution: simple line-edit merge.
- `tests/` *(directory, not individual files)* — both sessions **add new files** freely. Neither modifies existing test files owned by the other session.

---

## Active working

> Sessions update this section after each commit. Keep entries terse (one line each).
> When a session is idle / between turns, remove its entries.

### Session A — active

- `[2026-04-25 13:00] feat/f2-typed-event-bus-3a` touched `plugin_sdk/ingestion.py` (NEW), `plugin_sdk/__init__.py` (re-exports), `opencomputer/ingestion/{__init__,bus}.py` (NEW), `opencomputer/agent/loop.py` (publisher wiring in `_dispatch_tool_calls` + new `_emit_tool_call_event`), `docs/sdk-reference.md` (new Ingestion section), `docs/parallel-sessions.md` (this file — bus API change log entry + active-working entry), `CHANGELOG.md` (append [Unreleased] entry), `tests/test_typed_event_bus.py` (NEW — 22 tests), `tests/test_signal_normalizer.py` (NEW — 8 tests), `tests/test_loop_emits_bus_events.py` (NEW — 5 tests). Full suite at **1734 passing** (was 1699 entering 3.A). Ruff clean. **Session B B3 unblocked** — see Bus API change log below. Worktree at `/tmp/oc-3a`. **Zero Session-B or Session-C reserved files touched.**

### Session B — active

- `[2026-04-24 13:45] feat/hermes-evolution-b1` touched `opencomputer/evolution/*` (new subpackage), `docs/evolution/*` (new), `tests/test_evolution_*.py` (new — 5 files / 73 tests), `docs/parallel-sessions.md` (this file), `CHANGELOG.md` (append [Unreleased] entry). **Zero changes to Session-A-reserved files.** Working from git worktree at `/tmp/oc-evo` to avoid branch-cycling conflicts with Session A in the primary checkout.
- `[2026-04-24 14:30] feat/hermes-evolution-b2` (stacked off `feat/hermes-evolution-b1`) touched `opencomputer/evolution/{reflect,synthesize,cli,entrypoint}.py`, `opencomputer/evolution/prompts/{reflect,synthesize}.j2`, `tests/test_evolution_{reflect_template,reflect_engine,synthesize_skill,cli}.py` (4 new test files / 36 new tests; -1 obsolete B1 stub test), `docs/evolution/README.md` (B1 placeholder → B2 user docs), `CHANGELOG.md` (append B2 entry under [Unreleased]). Full suite at 1070 passing. Worktree at `/tmp/oc-evo-b2`. **Zero Session-A-reserved files touched** (`opencomputer/cli.py` NOT modified — Session A wires the subapp via one-line PR per `docs/evolution/README.md`). **MERGED 2026-04-24 12:51 as `59ffa7c`** after #41 (B1) merged as `d2b13ac`.
- `[2026-04-24 18:30] feat/hermes-evolution-b4` touched `opencomputer/evolution/{prompt_evolution,monitor}.py` (new), `opencomputer/evolution/migrations/002_evolution_b4_tables.sql` (new), `opencomputer/evolution/{storage,cli}.py` (extended), `tests/test_evolution_{storage_b4,prompt_evolution,monitor,cli_b4}.py` (4 new test files / 58 new tests), `docs/evolution/README.md` (B2 status → B4 status), `CHANGELOG.md` (append B4 entry). Full suite at 1326 passing. Worktree at `/tmp/oc-evo-b4`. **B3 explicitly skipped** — depends on Session A's `opencomputer/ingestion/bus.py` which doesn't exist yet on main. **Zero Session-A-reserved files touched.** **MERGED 2026-04-24 13:07 as `a4bbd17`.**

### Session C — active

- `[2026-04-25 09:00] feat/f6-f7-c1-deep-scans` touched `docs/f6/{opencli-source-map,design,README}.md` (new — 491-line OpenCLI deep-scan + design doc with full self-audit + adversarial review + user README), `docs/f7/{oi-source-map,design,README,interweaving-plan}.md` (new — 578-line OI deep-scan + design doc + user README + Phase 5 refactor contract for Session A), `docs/parallel-sessions.md` (this file — added Session C reserved-files block + active-working entry), `CHANGELOG.md` (append C1 entry under [Unreleased]). **Docs only — no code, no tests, no plugin scaffolding yet** (those are C2/C3). **Zero Session-A or Session-B reserved files touched.** Worktree at `/tmp/oc-c1`. **MERGED 2026-04-24 22:59 as `f33708d`.**
- `[2026-04-25 11:00] feat/f6-c2-opencli-plugin` touched `extensions/opencli-scraper/*` (new — wrapper, rate_limiter, robots_cache, field_whitelist, subprocess_bootstrap, 3 tools, plugin.{py,json}, LICENSE, NOTICE), `tests/test_opencli_*.py` (6 new test files / 85 tests). Full suite at 1442 passing. Worktree at `/tmp/oc-c2`. **Tools NOT registered** (plugin.py register stub returns early; Session A wires consent + signal-normalizer in Phase 4 + flips `enabled_by_default: true`). **Discrepancy noted**: `PluginManifest.kind` is `"tool"` (singular) but `plugin.json` uses `"tools"` (plural) — both coexist (loader reads raw JSON without validating). Worth picking one in a follow-up. **Zero Session-A or Session-B reserved files touched.** **MERGED 2026-04-24 23:22 as `21440a8`.**
- `[2026-04-25 13:00] feat/f7-c3-oi-plugin` touched `extensions/oi-capability/*` (new — subprocess wrapper + JSON-RPC protocol + telemetry kill-switch + venv bootstrap + 23 tools across 5 risk tiers + plugin.{py,json} + LICENSE + NOTICE), `tests/test_oi_*.py` (10 new test files / 162 tests including AGPL boundary CI guard), `tests/conftest.py` (NEW — handles hyphenated extension dir name `extensions/oi-capability/` → importable as `extensions.oi_capability`). Full suite at 1604 passing. Worktree at `/tmp/oc-c3`. **Tools NOT registered** (plugin.py stub returns early; Session A wires consent + sandbox + AuditLog + interweaves into coding-harness in Phase 5 per `docs/f7/interweaving-plan.md`). **AGPL boundary verified**: `import interpreter` appears in exactly 1 file (`subprocess/server.py`); CI test enforces this. **Telemetry kill-switch** patches `sys.modules["interpreter.core.utils.telemetry"]` BEFORE OI import + disables litellm telemetry. **Zero Session-A or Session-B reserved files touched.** **MERGED 2026-04-25 00:01 as `77cca65`.**
- `[2026-04-25 16:30] feat/f7-c5-oi-use-cases` touched `extensions/oi-capability/use_cases/*` (new — 8 use-case libraries: autonomous_refactor, life_admin, personal_knowledge_management, proactive_security_monitoring, dev_flow_assistant, email_triage, context_aware_code_suggestions, temporal_pattern_recognition), `tests/test_oi_use_cases_*.py` (8 new test files / 85 tests), `tests/conftest.py` (1-line addition: `"use_cases"` to sub-package alias loop). Full suite at 1819 passing. Worktree at `/tmp/oc-c5`. **NOT registered as tools** — composes C3 OI tool wrappers; Session A's Phase 5 will integrate with coding-harness per interweaving plan. **AGPL boundary holds** (no `import interpreter` anywhere; only goes through tool wrappers which use JSON-RPC subprocess). **Zero Session-A or Session-B reserved files touched.**

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

- `[2026-04-25]` Initial bus API shipped (Phase 3.A) — public types in `plugin_sdk/ingestion.py`: `SignalEvent` + 5 subclasses (`ToolCallEvent`, `WebObservationEvent`, `FileObservationEvent`, `MessageSignalEvent`, `HookSignalEvent`) + `SignalNormalizer` ABC + `IdentityNormalizer`. Bus singleton via `opencomputer.ingestion.bus.default_bus` (also reachable through `get_default_bus()`). Sync `publish` + async `apublish`; subscribers via `subscribe(event_type, handler)` / `subscribe_pattern("web_*", handler)` / `subscribe(None, handler)` for wildcard. `Subscription.unsubscribe()` is idempotent. **Session B may now subscribe.** B3's trajectory recorder should attach as `default_bus.subscribe("tool_call", recorder)` and rely on the per-subscription `BackpressurePolicy` enum for backpressure. The two `*SignalEvent` class names (vs `MessageEvent` / `HookEvent`) are an intentional collision-avoidance choice — discriminator strings (`"message"`, `"hook"`) match the original phase-3.A spec naming.

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
