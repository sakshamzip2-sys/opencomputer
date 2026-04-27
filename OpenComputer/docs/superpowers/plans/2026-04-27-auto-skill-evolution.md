# Auto Skill Evolution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Ship the auto-skill-evolution loop closing OC's only real gap vs. Hermes's "self-improving skills" claim. Detect successful patterns → extract SKILL.md candidates → stage in `_proposed/` for review → CLI for accept/reject. Default OFF; user opt-in.

**Architecture:** New `extensions/skill-evolution/` plugin (~1,100 LOC) + new `SessionEndEvent` SDK type + 3 F1 capabilities + `oc skills` CLI subcommand group. Reuses existing F2 bus, cost-guard, sensitive-app filter, provider plugins, capability gate.

**Spec:** `OpenComputer/docs/superpowers/specs/2026-04-27-auto-skill-evolution-design.md`
**Branch:** `feat/auto-skill-evolution` (worktree at `/tmp/oc-skill-evo/`)

---

## Tasks

### T1: SDK SessionEndEvent + 3 F1 capabilities + agent loop emission

- Add `SessionEndEvent(SignalEvent)` to `plugin_sdk/ingestion.py` with fields: `event_type="session_end"`, `end_reason: str = "completed"`, `turn_count: int = 0`, `duration_seconds: float = 0.0`, `had_errors: bool = False`.
- Register in `opencomputer/agent/consent/capability_taxonomy.py`:
  - `skill_evolution.observe` → `ConsentTier.IMPLICIT`
  - `skill_evolution.propose` → `ConsentTier.EXPLICIT`
  - `skill_evolution.auto_publish` → `ConsentTier.PER_ACTION`
- Emit `SessionEndEvent` in `opencomputer/agent/loop.py` near where `set_session_title` fires (TS-T6 location). Track turn count + duration during the run; emit via `default_bus.apublish()`.
- Tests (`tests/test_skill_evolution_sdk.py`): event shape (frozen, slots, defaults safe); capability taxonomy entries; event is emitted on END_TURN (mock the bus, run a session, assert publish call).

**Acceptance:** 5+ tests pass; event fires on session end; capabilities registered.

### T2: Pattern detector (heuristic + LLM-judge stub)

- Create `extensions/skill-evolution/pattern_detector.py`.
- `is_candidate_session(session_id, db, existing_skills, sensitive_app_filter) -> CandidateScore` — runs the heuristic stage-1 filter:
  - Reject if `had_errors` or `turn_count < 3`
  - Reject if any tool call referenced sensitive-app foreground events (look up via F2 bus archive or session metadata)
  - Reject if SessionDB FTS5 finds a high-similarity match against an existing skill description (cosine > 0.8 via simple TF-IDF or via Honcho if available)
  - Reject if user messages are <50 chars total (conversational filler)
- `judge_candidate_async(score, transcript_summary) -> JudgeResult` — stage-2 LLM call:
  - Constructs a prompt with the session summary + existing skill names
  - Calls cheap model (`claude-haiku-4-5-20251001`) via existing provider plugin
  - Parses structured response: `{confidence: int, novel: bool, reason: str}`
  - Cost-guarded: pre-flight check via `opencomputer.cost_guard`; daily limit `max_judge_calls_per_day=20` (configurable)
- Tests (`tests/test_skill_evolution_pattern_detector.py`):
  - 4 stage-1 reject cases (errors / short / sensitive / duplicate)
  - 1 stage-1 pass case
  - 2 stage-2 cases (mocked LLM): confidence above + below threshold
  - 1 cost-guard test (budget exhausted → no LLM call)

**Acceptance:** 8+ tests pass; heuristic correct; LLM judge mockable.

### T3: Skill extractor

- Create `extensions/skill-evolution/skill_extractor.py`.
- `extract_skill_from_session(session_id, db, judge_result) -> ProposedSkill` returns dataclass `ProposedSkill(name, description, body, provenance)`.
- 3 LLM calls (cheap model, all cost-guarded):
  1. **Intent**: "summarize what the user was trying to do in 1 sentence"
  2. **Procedure**: "summarize the agent's successful procedure as numbered steps; redact paths/secrets"
  3. **Trigger description**: "phrase as 'Use when [user request shape]' for the SKILL.md frontmatter"
- Compose SKILL.md in OC's standard format (frontmatter `name` + `description` + body). Generated name: `auto-{session_id_prefix(8)}-{slug(intent, max=30)}`.
- Inject sensitive-app filter from `extensions.ambient_sensors.sensitive_apps` for any path/identifier in the body.
- Tests (`tests/test_skill_evolution_skill_extractor.py`):
  - happy path: mock 3 LLM calls, verify SKILL.md shape
  - sensitive-app path filtered out of body
  - generated name follows pattern + slug rules
  - LLM error handling (one call fails → returns None, logs)

**Acceptance:** 5+ tests pass; generated SKILL.md matches OC schema; redaction works.

### T4: Candidate store (file-based stage/list/accept/reject)

- Create `extensions/skill-evolution/candidate_store.py`.
- `<profile_home>/skills/_proposed/<name>/SKILL.md` + `<profile_home>/skills/_proposed/<name>/provenance.json`.
- API:
  - `add_candidate(profile_home: Path, proposal: ProposedSkill)` — atomic write
  - `list_candidates(profile_home) -> list[CandidateMetadata]`
  - `get_candidate(profile_home, name) -> ProposedSkill | None`
  - `accept_candidate(profile_home, name) -> Path` — moves `_proposed/<name>/` to `<profile_home>/skills/<name>/`; returns the new path. Fails if name collides with active skill.
  - `reject_candidate(profile_home, name) -> bool` — deletes `_proposed/<name>/`; returns True if found.
  - `prune_old_candidates(profile_home, max_age_days=90) -> int` — auto-deletes proposals older than threshold; returns count pruned.
- Tests (`tests/test_skill_evolution_candidate_store.py`): 8 tests covering each operation + collision + pruning.

**Acceptance:** 8 tests pass; atomic writes; no data loss on accept/reject.

### T5: Bus subscriber + daemon lifecycle

- Create `extensions/skill-evolution/subscriber.py`.
- `EvolutionSubscriber` subscribes to `SessionEndEvent` via `default_bus.subscribe("session_end", handler)`.
- On event: if state.enabled, spawn an async background task (via `fire_and_forget`) that runs:
  1. Detector stage 1 (synchronous, fast)
  2. If pass: detector stage 2 (LLM judge, async)
  3. If pass: extractor (3 LLM calls, async)
  4. If success: `add_candidate()` to store
- State: `<profile_home>/skills/evolution_state.json` with `{enabled: bool}` (default disabled).
- Heartbeat: `<profile_home>/skills/evolution_heartbeat` (timestamp file, written on each event handled).
- Failure-isolated: every step wrapped in try/except; errors logged at WARNING; never raises into bus.
- Tests (`tests/test_skill_evolution_subscriber.py`):
  - subscriber wires to bus on plugin load
  - disabled state → no work done (heuristic check)
  - happy path: SessionEndEvent → detector → judge → extractor → candidate added (all mocked)
  - heartbeat written
  - exception in detector doesn't crash bus

**Acceptance:** 6 tests pass; subscriber lifecycle correct.

### T6: CLI — `oc skills` subcommand group

- Create `opencomputer/cli_skills.py` (matches `cli_ambient.py` shape).
- Subcommands:
  - `oc skills list` — show active + proposed (proposed marked clearly)
  - `oc skills review` — interactive review: shows each candidate's name + description + confidence; prompts accept/reject/skip
  - `oc skills accept <name>` — `accept_candidate()`
  - `oc skills reject <name>` — `reject_candidate()`
  - `oc skills evolution on` / `off` — flip state.enabled
  - `oc skills evolution status` — aggregate counts (proposed, accepted-rate-7d, last-event-ts)
- Mount in `opencomputer/cli.py` as `app.add_typer(skills_app, name="skills")`.
- Tests (`tests/test_skill_evolution_cli.py`):
  - on/off persist state
  - list shows both active + proposed
  - accept moves file
  - reject deletes
  - status output is aggregate-only (no specific session content)

**Acceptance:** 8 tests pass; CLI mounts; status output never leaks specifics.

### T7: Plugin manifest + plugin.py + privacy contract test

- Create `extensions/skill-evolution/plugin.json` (`enabled_by_default: false`, `kind: "mixed"`).
- Create `extensions/skill-evolution/plugin.py::register(api)` — wires the subscriber if `state.enabled`.
- Create `tests/test_skill_evolution_no_raw_transcript.py` — load a candidate, verify NO raw session transcript file in `_proposed/<name>/`. Only SKILL.md + provenance.json.
- Create `tests/test_skill_evolution_no_egress.py` — AST-scan plugin source for HTTP-client imports (mirroring `tests/test_ambient_no_cloud_egress.py`). LLM calls go via existing provider plugins, not direct HTTP — so the plugin source itself should have ZERO httpx/requests/aiohttp imports.

**Acceptance:** Both privacy tests pass with 0 violations.

### T8: Doctor + gateway hook + CHANGELOG + push + PR

- Add `_check_skill_evolution_state` to `opencomputer/doctor.py` (mirroring `_check_ambient_state` shape from PR #184).
- In `opencomputer/gateway/server.py`, after the ambient daemon hook, add:
  ```python
  try:
      from extensions.skill_evolution.plugin import start_subscriber_if_enabled
      start_subscriber_if_enabled(default_bus, _home())
  except Exception:
      _log.exception("skill-evolution subscriber failed to start; gateway continues")
  ```
- Add CHANGELOG entry with privacy-contract section.
- Extend `.github/workflows/test.yml::test-cross-platform` pytest pattern to include `tests/test_skill_evolution_*.py`.
- Push, open PR, wait for CI, merge.

**Acceptance:** doctor check shows status; gateway starts subscriber when enabled; CI green on all 3 OSes; CHANGELOG entry present.

---

## Self-Audit

### Flawed assumptions

| # | Assumption | Reality | Mitigation |
|---|---|---|---|
| FA1 | "LLM judge is cheap." | claude-haiku is ~$0.25/M tokens but a session summary + skill list + judge prompt = ~3K tokens × 2 calls × 10/day = 60K tokens/day = ~$0.015/day. Acceptable. ✓ | Cost-guard configurable. |
| FA2 | "Daily judge budget of 20 is enough." | If user has many short sessions (e.g. 50/day on Telegram), 20 judge calls means 30+ skipped sessions. Possibly too low. | Make configurable; document trade-off in README. |
| FA3 | "FTS5 dedup correctly identifies duplicate skill ideas." | FTS5 is keyword-based; might miss semantic duplicates ("write tests" vs "create unit tests"). | Use existing BGE embeddings + Chroma if available (already in V2.B); fall back to FTS5. |
| FA4 | "Generated `auto-*` names never collide." | Collision possible if same intent slug repeats. Need uniqueness suffix. | Append timestamp suffix on collision: `auto-{prefix}-{slug}-{N}`. |
| FA5 | "User will run `oc skills review` regularly." | Realistically: half won't, proposals pile up. | `oc skills evolution status` warns at >20 unreviewed; optional weekly digest via `oc skills evolution digest`. |
| FA6 | "LLM judge respects confidence threshold honestly." | LLMs over-claim. Confidence 70 might be 90 false-positive. | Calibrate by including 2-3 negative examples in the judge prompt; track real accept rate over time and surface in status. |
| FA7 | "All sessions on the bus emit SessionEndEvent." | Currently they don't — this is new. | T1 explicitly adds the emission point; tests verify. |
| FA8 | "Sensitive-app filter from ambient module covers everything." | The filter is for app names + window titles, not full session content. Bank-account-numbers in a chat message would slip through. | Add a basic content-redaction pass (regex for credit-card / SSN patterns) in extractor. Light-touch v1; full PII detection is deferred. |

### Edge cases

- EC1: Session ends with `had_errors=True` but the user RECOVERED and the recovery itself is the pattern. Heuristic rejects too eagerly. **Mitigation**: extend stage-1 to allow had_errors=True if final turn_count_after_error ≥ 2 (signal that recovery happened).
- EC2: User has no profile — runs ad-hoc CLI sessions. `<profile_home>/skills/_proposed/` doesn't exist yet. **Mitigation**: `add_candidate` creates parent dir.
- EC3: Two simultaneous SessionEndEvents (Telegram + CLI session both end). Race on candidate file write. **Mitigation**: `add_candidate` uses `tempfile.mkstemp` + `os.replace` for atomicity.
- EC4: User accepts a candidate, then runs `oc skills evolution off`. Already-accepted skill stays active. **Mitigation**: design contract — accept is one-way; state flag only gates new proposals.
- EC5: User accepts a candidate whose name conflicts with a curated bundled skill. **Mitigation**: `accept_candidate` checks active dir first; refuses on collision; suggests rename.
- EC6: Candidate generation fails mid-way (one LLM call OK, second fails). Half-written `_proposed/<name>/` left over. **Mitigation**: extractor uses tempdir + `os.rename` to atomic publish.
- EC7: User deletes `_proposed/` manually. State on next event handler call assumes dir exists. **Mitigation**: `mkdir(parents=True, exist_ok=True)` defensive.
- EC8: Bus subscription leaks on plugin reload. **Mitigation**: store subscription handle; `unregister()` on plugin teardown.
- EC9: F1 ConsentGate denies `skill_evolution.propose`. Subscriber tries each event but always blocked. **Mitigation**: log once at WARN and back off (don't spam); user can grant via CLI.
- EC10: SessionEndEvent fires with `turn_count=0` (session never started). **Mitigation**: stage-1 reject < 3.

### Missing considerations

- MC1: **Internationalization** — LLM judge prompt assumes English. Other-language sessions might trip on the prompt. Acceptable for v1; document.
- MC2: **Skill versioning** — accepting an `auto-` skill that has the same intent as one accepted last week. Not tracked. Future: a "merge this proposal with skill X" CLI option.
- MC3: **Skill QUALITY scoring** — accepted skills currently can't be downgraded. Future: track usage + success rate; prune low-performing ones.
- MC4: **Notification hook** — user wouldn't know a candidate was generated until they run `oc skills review`. Add: optional Telegram nudge when proposed count crosses a threshold (re-uses PushNotification tool).
- MC5: **Cross-profile sharing** — accepted skills only apply per-profile. Sharing requires manual copy. Acceptable for v1.
- MC6: **The CI matrix** — `tests/test_skill_evolution_*.py` need to be added to the cross-platform CI pattern (same gotcha as ambient PR #184).

### Alternatives considered

- AA1: Auto-publish on confidence ≥ 95 (no review). **Rejected**: even at 95, false-positives accumulate fast. Review is the right default.
- AA2: Persist raw session transcript with the candidate. **Rejected**: transcript may contain sensitive content; we already have it in SessionDB; storing twice is wasteful.
- AA3: Use embeddings for dedup (semantic). **Partial**: prefer if BGE/Chroma available; FTS5 fallback.
- AA4: Run extraction synchronously in the agent loop before END_TURN. **Rejected**: latency. Async via fire_and_forget is correct.
- AA5: Use OpenAI embeddings for dedup. **Rejected**: adds network dep + cost; local BGE is fine.

### Refinements applied

1. **EC1**: extend stage-1 to allow had_errors=True if recovery signal present.
2. **FA4**: candidate name uniqueness via timestamp suffix on collision.
3. **EC6**: extractor uses tempdir + os.rename for atomicity.
4. **MC4**: add optional Telegram nudge (configurable, default OFF) — defer to T8 polish if time.
5. **MC6**: explicit CI matrix update in T8.

### Effort estimate (post-audit)

| Task | Effort |
|---|---|
| T1 SDK + capabilities + emission | 45m |
| T2 Pattern detector | 75m |
| T3 Skill extractor | 90m |
| T4 Candidate store | 60m |
| T5 Subscriber + lifecycle | 75m |
| T6 CLI | 75m |
| T7 Plugin manifest + privacy tests | 45m |
| T8 Doctor + gateway + CHANGELOG + PR | 60m |
| **Total** | **~8 hours** |

### Acceptance criteria (merge bar)

- [ ] All 8 task acceptance criteria met.
- [ ] `pytest tests/test_skill_evolution_*.py` green.
- [ ] `tests/test_skill_evolution_no_egress.py` finds 0 HTTP-client imports.
- [ ] `tests/test_skill_evolution_no_raw_transcript.py` enforces no raw transcript persisted.
- [ ] CHANGELOG entry under `[Unreleased]` with privacy-contract section.
- [ ] CI matrix extended to include `test_skill_evolution_*.py` on all 3 OSes.
- [ ] PR body links spec + plan.

---

*Plan with self-audit complete. Ready for subagent-driven execution.*
