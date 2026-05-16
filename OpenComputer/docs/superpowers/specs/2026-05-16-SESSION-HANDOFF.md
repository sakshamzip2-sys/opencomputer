# 2026-05-16 — Session Handoff

**Read this first.** This file is the single entry point for picking up where the previous session left off. Everything else is linked from here.

---

## TL;DR — Status at handoff

| Track | Status | Action needed |
|---|---|---|
| Parity plan (Hermes + OpenClaw → OC) — 2 spec files | **Written, ready to execute** | Hand to Claude Code; it has the full task list and pre-flight checklist |
| Awareness cleanup plan — 2 spec files | **Written, ready to execute** | Hand to Claude Code; explicitly post-parity-plan, do not run concurrently |
| Slash `/fork` → session-fork-helper refactor | **Shipped (uncommitted)** | Verify + commit when satisfied |
| Full `pytest` run | **Incomplete — timed out at 10 min** | Re-run in background (see follow-up #1); may block Track A |
| Pre-existing test failure | **Pre-existing, not from this session** | File a separate bug (see follow-up #2) |
| MEMORY.md "grep behaviour not name" rule | **Added** (one older rule dropped on compaction — see MEMORY.md section below) | Decide whether to restore the dropped rule from `.bak` |
| Dashboard fork migration (3rd fork-logic copy) | **Documented, deliberately deferred** | See follow-up #1 below; small ticket |
| CLI `--record-parent` flag for `oc session fork` | **Documented, deferred** | See follow-up #2; trivial |

Nothing committed. Per the working rules, the previous session did not `git commit`. Everything is in your working tree.

### Pre-existing untracked items in `git status` (NOT from this session)

When you run `git status` you will see these untracked items. **None of them were created by this session's work** — they existed on disk before any of the changes documented in this handoff. Do not commit them as part of the work below; treat them as separate state to investigate or `.gitignore`:

- `opencomputer/oc-workspace/` — pre-existing
- `opencomputer/open-design/` — pre-existing
- `.claude/` — pre-existing

The files this session actually produced are listed under "What's on disk" below. Everything else in `git status` predates this work.

---

## Session goal — what was being attempted

The user (Saksham) wanted three things during this session:

1. **"Make OC competitive with Hermes and OpenClaw."** Specifically: port what's actually missing, not what I claimed was missing. Output: a detailed plan in `2026-05-16-oc-parity-with-hermes-openclaw/` covering sandbox-scope policy, tool-loop detection, E2B ephemeral sandbox backend + resolver, Microsoft Graph client (mail/cal/drive), NeuTTS local voice, and reference-repo extraction for the explicitly-deferred items (fleet routing, full-duplex voice-call, sandboxed browser + noVNC).

2. **"Improve the layered-awareness graph."** Audit what's built vs. what's actually wired into the prompt; write a plan to fix the writer-side junk leakage, add a context-aware reranker, ship a `oc awareness review/forget/correct/explain` CLI, and wire the existing decay + drift subsystems into the reranker. Output: plan in `2026-05-16-awareness-cleanup/`. Explicitly post-parity-plan.

3. **"Add a `/fork` slash command."** Discovered mid-task that `/branch` already does this, so the work pivoted into a useful DRY refactor: extract a shared `fork_session()` helper, migrate both `oc session fork` (CLI) and `/branch` (slash) to use it, write 21 unit tests for the helper directly, document the dashboard's third fork-logic copy as a follow-up.

All three goals delivered. See "What's on disk" below for the full inventory.

---

## What's on disk (verified, uncommitted)

### Spec files — `docs/superpowers/specs/`

| Path | Lines | Purpose |
|---|---|---|
| `2026-05-16-oc-parity-with-hermes-openclaw/PART-1-brainstorm-and-audit.md` | 279 | 8 approaches considered, scored, converged on Approach H. Phase 2 audit covers 9 lenses. Pre-work table corrects earlier "Hermes-only feature" hallucinations. Honest framing of what Hermes actually does vs. what OC's plan adds. |
| `2026-05-16-oc-parity-with-hermes-openclaw/PART-2-plan-and-plan-audit.md` | 245 | 5 milestones, MVP marked. Phase 4 audit pass found 5 undersized tasks and raised the total estimate from 5–6 weeks to 6–7. Explicit deferrals list. Pre-flight checklist. |
| `2026-05-16-awareness-cleanup/PART-1-brainstorm-and-audit.md` | 261 | Pre-work table inventories what's actually built (decay engine, drift store, persona classifier, all dormant) vs. the 30-LOC ranker that ignores them. 8 approaches, converged on H. |
| `2026-05-16-awareness-cleanup/PART-2-plan-and-plan-audit.md` | 219 | 5 milestones. MVP = audit + correction CLI (M1) so subsequent changes are validatable. Hard 80%-exit criterion on writer cleanup. Explicit post-parity-plan dependency. |
| `2026-05-16-slash-fork/PLAN.md` | 51 | Started as "/fork doesn't exist" (wrong — `/branch` does it). Pivoted to a DRY refactor. Final state documented as SHIPPED with three follow-ups. |
| `2026-05-16-SESSION-HANDOFF.md` | this file | Entry point for next session |

### Code changes — `opencomputer/`

| File | Type | LOC | Status |
|---|---|---|---|
| `opencomputer/agent/session_fork.py` | New helper | 161 | `ruff` clean; 21 dedicated unit tests passing |
| `opencomputer/agent/slash_commands_impl/branch_cmd.py` | Migrated to use helper | 94 (was 115) | All 13 existing tests pass |
| `opencomputer/cli_session.py::session_fork` | Migrated to use helper | unchanged net | All 57 CLI session tests pass |
| `opencomputer/dashboard/routes/hermes_aliases.py` | Added NOTE comment pointing at the helper | +9 | Behaviour unchanged (deliberately) |

### Tests — `tests/agent/`

| File | Tests | Status |
|---|---|---|
| `tests/agent/test_session_fork_helper.py` | 21 new | All passing |

### MEMORY.md

- New rule added: "Behavioral rule: grep behaviour, not name (2026-05-16)". Documents the failure mode that produced this session's pivot (read command names but not docstrings, shipped a duplicate).
- Memory file auto-compacted on write at the time the new rule was added. The compactor reported "DROPPED 3 entries" but only **one** load-bearing rule actually went away (see below). Pre-compaction state preserved at `~/.opencomputer/MEMORY.md.bak`.
- **What was dropped (verified by diffing `MEMORY.md` vs `MEMORY.md.bak`):**
  - **The "finish the task, don't escalate the failure" behavioural rule (dated 2026-04-29).** This is a real loss — it's a 12-line rule about not stopping after a single tool failure and pushing through fallback ladders before escalating. If the next session sees the agent narrate failures instead of trying alternatives, this rule needs to be re-added. Full text recoverable from `MEMORY.md.bak`.
  - Two other entries — the existing "guessed model name" rule and "Number 17" entry — were **preserved** in the current `MEMORY.md`; the compactor's "3 entries" count is misleading.
- **Recovery action for next session:** decide whether to restore the dropped rule by appending its body back into `MEMORY.md` (3666 / 4000 chars currently used → ~330 chars budget remains; the rule is ~1000 chars so something else has to go to fit it). Otherwise, the failure-escalation rule lives only in `.bak` going forward.

---

## What was *not* finished

### Open items, ranked by urgency

#### 1. Full pytest run never finished (10-minute timeout)

Status: **incomplete.** Partial run got to 25% (4050+ tests passed) before stop-on-first-fail tripped on one failure (see #2). Re-running with that failure deselected timed out at 10 minutes wallclock without reaching the end. The local affected-scope sweep — 651 tests in `tests/agent`, `tests/tier2_slash`, `tests/cli_ui` — was green; the failures (if any) in the unreached 75% are unknown.

**Caveat for Track A below:** running pytest is step 1 of Track A. If the full suite consistently takes >10 minutes on your machine, it will block the track. Plan accordingly — either run pytest in the background with `nohup` while continuing other work, or scope the verification to the affected-paths sweep (which is what this session relied on).

```bash
# Background invocation that survives the agent timeout
cd /Users/saksham/Vscode/claude/OpenComputer
nohup .venv/bin/python -m pytest -q \
  --deselect tests/test_cli_first_run_offer.py::test_has_any_provider_configured_false_when_no_keys \
  --ignore=tests/test_e2e \
  --timeout=60 \
  > /tmp/oc-pytest-handoff.log 2>&1 &
# Then `tail -f /tmp/oc-pytest-handoff.log` to watch progress.
```

#### 2. Pre-existing test failure (independent of this session)

Status: **bug to file; not caused by this work.**

The failure: `tests/test_cli_first_run_offer.py::test_has_any_provider_configured_false_when_no_keys`. Test deletes 4 env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AWS_BEDROCK_ACCESS_KEY_ID`, `ANTHROPIC_BASE_URL`) and expects `cli._has_any_provider_configured()` to return False. Returns True.

**Verified pre-existing:** reproduced on clean checkout with all session work stashed.

**Likely cause:** the function now checks additional sources (config file, keyring, or more env vars) that the test's monkeypatch doesn't cover. Either fix the function to only check what the test removes, or update the test to monkeypatch the additional sources. Out of scope for this session's work — file as a separate bug.

#### 3. Dashboard fork-logic third copy

Location: `opencomputer/dashboard/routes/hermes_aliases.py::fork_session` (lines 320–389).

Why deferred: different default-title shape (`"Fork of <id>"` vs. `"<src> (fork)"`), HTTPException wrapping, and raw SQL for timestamp preservation. The raw-SQL bit is no longer needed because `SessionDB.append_messages_batch` honours `msg.timestamp` since 2026-05-11 (see `state.py:2278`). Migration is *possible* now, just out of scope for the BranchCommand pass.

A NOTE comment is already in the file pointing at the helper. Estimated migration size: **S (half-day)** including its own test.

#### 4. `oc session fork --record-parent` CLI flag

The shared helper supports `record_parent: bool = False`. The slash command opts in (`record_parent=True`); the CLI does not (preserves pre-Phase-H behaviour). Adding a CLI flag `--record-parent` is trivial:

```python
# in opencomputer/cli_session.py session_fork():
record_parent: bool = typer.Option(False, "--record-parent", help="..."),
# then pass record_parent=record_parent to fork_session(...)
```

Estimated: **XS (15 min)** including test.

#### 5. Commit the work

Nothing is committed. Per working rules, the previous session did not `git commit`. Suggested commit groups:

```bash
# Group 1: the spec files (planning artifacts)
git add docs/superpowers/specs/2026-05-16-oc-parity-with-hermes-openclaw/
git add docs/superpowers/specs/2026-05-16-awareness-cleanup/
git add docs/superpowers/specs/2026-05-16-slash-fork/
git add docs/superpowers/specs/2026-05-16-SESSION-HANDOFF.md
git commit -m "docs(plans): 2026-05-16 parity + awareness + fork-dedup specs"

# Group 2: the session-fork helper (the actual code work)
git add opencomputer/agent/session_fork.py
git add opencomputer/agent/slash_commands_impl/branch_cmd.py
git add opencomputer/cli_session.py
git add opencomputer/dashboard/routes/hermes_aliases.py
git add tests/agent/test_session_fork_helper.py
git commit -m "refactor(session-fork): extract shared helper; /branch + oc session fork use it"
```

The two groups are independent. Either can ship without the other.

---

## What the next Claude Code session should do

Pick one of these tracks; do not run them concurrently.

### Track A: ship the helper refactor

1. Re-run the full pytest (command above). Confirm only the known pre-existing failure remains.
2. Review the diff manually (`git diff opencomputer/agent/session_fork.py opencomputer/agent/slash_commands_impl/branch_cmd.py opencomputer/cli_session.py opencomputer/dashboard/routes/hermes_aliases.py tests/agent/test_session_fork_helper.py`).
3. Commit per groups above.
4. (Optional) Knock out follow-up #4 (`--record-parent` flag) — 15 min.
5. (Optional) Knock out follow-up #3 (dashboard migration) — half-day.

### Track B: execute the parity plan

1. Read `docs/superpowers/specs/2026-05-16-oc-parity-with-hermes-openclaw/PART-1-brainstorm-and-audit.md` end-to-end.
2. Read `PART-2-plan-and-plan-audit.md` end-to-end.
3. Run the pre-flight checklist in §4.7 of PART-2.
4. Start Milestone 1 (Sandbox scope + tool-loop detection — the MVP).
5. Each task in the milestone has explicit `size`, `deps`, `risks` columns. Follow the order.

The plan deliberately leaves three big items (fleet routing, sandboxed-browser + noVNC, full-duplex voice-call) out of v1 and documents *why*. If you (the user) want to swap one in, see PART-2's §4.6 — that section shows what's tradeable.

### Track C: execute the awareness cleanup plan

**Do not run this concurrently with Track B.** PART-1 §"Pre-flight" explicitly says this is post-parity-plan.

1. Read `docs/superpowers/specs/2026-05-16-awareness-cleanup/PART-1-brainstorm-and-audit.md`.
2. Read `PART-2-plan-and-plan-audit.md`.
3. Pre-flight checklist confirms parity plan has shipped.
4. Start Milestone 1 (Audit + correction CLI — the MVP).

The pre-work table in PART-1 is load-bearing: it inventories what's already built (~2200 LOC of dormant subsystems) so the next engineer doesn't redesign them.

---

## Important conventions learned this session

These are documented in the artifacts above but worth surfacing for the next session:

1. **MEMORY.md rule #3 (new this session):** grep on behaviour, not name. When auditing "does X exist?", read docstrings for candidate name-matches; surgical name-grep alone is a footgun.

2. **MEMORY.md rule #1 (older, repeatedly relevant):** never claim a checkable fact about the codebase without running the tool that verifies it. The previous session burned this rule twice: once claiming features were Hermes-only when OC already had them (MoA, OSV scan, Tirith, batch runner), once claiming `/fork` was missing when `/branch` did it. Both were caught and corrected.

3. **Hermes' multi-backend pattern ≠ what OC plans:** the parity plan's M2 description explicitly corrects an earlier "Hermes-style" framing. Hermes ships 8 sandbox-backend files but uses them only through one tool (`terminal_tool.py`) with `os.getenv("TERMINAL_ENV")` read once at startup. OC's M2 adds per-tool routing on top of the file-layout pattern — that's OC-original, not a port.

4. **Crabbox was considered and rejected:** for the agent use case (ephemeral per-call containers), the right tool is E2B. Crabbox is a CI-runner tool (full-VM lease for minutes-to-hours). Don't relitigate; the plan's §"Explicitly out of scope" names this explicitly.

5. **Plan files are split into PART-1 + PART-2** when they exceed ~250 lines per file. PART-1 is brainstorm + design audit (phases 1+2); PART-2 is plan + plan audit (phases 3+4). Read in order.

---

## File tree of new artifacts

```
docs/superpowers/specs/
├── 2026-05-16-SESSION-HANDOFF.md           ← you are here
├── 2026-05-16-oc-parity-with-hermes-openclaw/
│   ├── PART-1-brainstorm-and-audit.md       (279 lines)
│   └── PART-2-plan-and-plan-audit.md        (245 lines)
├── 2026-05-16-awareness-cleanup/
│   ├── PART-1-brainstorm-and-audit.md       (261 lines)
│   └── PART-2-plan-and-plan-audit.md        (219 lines)
└── 2026-05-16-slash-fork/
    └── PLAN.md                              (51 lines)

opencomputer/
├── agent/
│   ├── session_fork.py                      (NEW — 161 lines)
│   └── slash_commands_impl/
│       └── branch_cmd.py                    (MIGRATED — 94 lines, was 115)
├── cli_session.py                           (MIGRATED — uses helper)
└── dashboard/routes/
    └── hermes_aliases.py                    (NOTE added — behaviour unchanged)

tests/agent/
└── test_session_fork_helper.py              (NEW — 21 tests, all passing)
```

---

## One-liner status, for the impatient

> Two detailed multi-week plans written (parity + awareness) + one small refactor shipped (fork-helper dedup). Nothing committed. Full pytest didn't finish; one pre-existing failure flagged. Three small follow-ups documented. Ready for either commit-and-ship, or pick up Track B (parity) or Track C (awareness).

---

## RESOLUTION — 2026-05-16 (follow-up session)

Track A is fully shipped; handoff open items #1–#4 are all closed.
Tracks B + C were audited this session (not worked on): Track C is
essentially done, Track B is missing one milestone (M4) — see below.

**Track A — session-fork dedup, complete.** Branch
`feat/session-fork-helper-2026-05-16`, three commits on `origin/main`:

1. `refactor(session-fork): extract shared fork_session helper` — the
   helper + `/branch` + `oc session fork` migrated to it (this was the
   uncommitted work at handoff time).
2. `feat(session-fork): add oc session fork --record-parent flag` —
   open item #3 / follow-up #2. TDD.
3. `refactor(session-fork): migrate dashboard /fork endpoint to shared
   helper` — open item #2 / follow-up #1. The third fork-logic copy
   retired. Added a `fallback_title` helper param; the migration also
   fixed two latent bugs — the dashboard fork dropped the source
   `model`, and its raw SQL dropped `reasoning` + `attachments`.

All three fork call sites — CLI, `/branch` slash, dashboard endpoint —
now route through one tested `fork_session` helper.

**Open item #1 — full pytest + the pre-existing failure.** Resolved.
The full suite was re-run (`--ignore=tests/test_e2e`): **16,131
passed, 32 skipped, 6 xfailed**, 7m35s, zero failures. The flagged
`test_has_any_provider_configured_false_when_no_keys` failure **no
longer reproduces** — it passes in isolation and in-suite on the
current `origin/main` base. No bug to file; it self-resolved on a
newer base.

**Tracks B + C — status snapshot.** B/C move independently of this
branch; `git log origin/main` is the live source of truth. As of this
session's end:

*Track B — parity plan, 5 milestones:* M1 (#623, sandbox scope +
tool-loop), M2 (#626, E2B backend + resolver), M3 (#627, Microsoft
Graph tool — merged mid-session), and M5 (#622, reference extractions)
shipped. **M4 (NeuTTS local voice) is the one milestone not yet on
`origin/main`.** Its scope is in
`2026-05-16-oc-parity-with-hermes-openclaw/PART-2` (Milestone 4).

*Track C — awareness cleanup, 5 milestones:* PR #625's squash body
shows it shipped M1 (inspection CLI), M2 (writer cleanup + validator),
M3 (context-aware reranker), and M4 (decay + drift wired into the
reranker). M5 (observability + top-level docs) was not separately
confirmed — at most a small (~2-day, docs-only) gap. Track C is
essentially done.

Plus a config fix (#624). Process note: an earlier draft of this
RESOLUTION called B+C "merged" with no verification — an overclaim,
caught and corrected; this snapshot supersedes it.

**Branch note.** The branch was rebased to drop a stray inherited
commit (`85174ce2`, a life-event-teeth plan doc) so it carries
fork-helper work only. That commit object is preserved — `git
cherry-pick 85174ce2` recovers it if the life-event-teeth track needs
it.
