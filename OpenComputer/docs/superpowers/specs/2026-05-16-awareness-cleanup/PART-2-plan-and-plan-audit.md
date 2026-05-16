# Layered Awareness cleanup — Part 2: Plan & Plan Audit

Date: 2026-05-16
Owner: Saksham
Companion file: `PART-1-brainstorm-and-audit.md` — read first.

Approach: **H — Writer cleanup (B) + context-aware reranker (C) + correction CLI (D); defer persona/life-events teeth (E) to v2.**

---

## Phase 3 — /plan

### "Done" in one sentence

OC's per-chat user-facts block contains relevant, deduped, decay-aware top-K facts ranked by session context (not by static kind-priority); the writer rejects mis-classified inputs at source; the user has `oc awareness {review, forget, correct, explain}` to inspect and fix the graph; and `pytest` + `ruff` are green.

### Milestones

#### Milestone 1 — Audit + correction CLI (LOAD-BEARING, **MVP**)

This is the MVP. Ships **before any other change** because the user needs the inspection tooling to validate that subsequent changes actually help. Done when: `oc awareness review` shows current top-K with provenance, `oc awareness forget <id>` evicts a fact (with `--confirm` for identity nodes), `oc awareness explain <id>` shows full provenance, and a writer-audit report tells us how dirty the graph is.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T1.1 Read `user_model/store.py`, `decay.py`, `drift.py`, `context.py` end-to-end; write a one-page implementation summary at `docs/refs/oc-user-model-baseline.md` covering what's wired and what's dormant | S | — | If decay or drift turns out broken, M2/M3 estimates shift. **Gate further work on this.** |
| T1.2 Grep every writer of `kind="preference"` / `"goal"` / `"identity"` / `"attribute"` nodes; produce `docs/refs/oc-user-model-writers.md` listing each writer + what it writes + which look mis-classified | S | T1.1 | If the list is huge (>10 writers), Milestone 2 grows |
| T1.3 Implement `oc awareness review` — Rich table of (node_id, kind, value, confidence, last_seen, source, incoming_contradicts_count). Top-K=50 by default; `--all` for full dump | M | T1.1 | UX: must be readable; mirror `oc consent list` shape |
| T1.4 Implement `oc awareness explain <id>` — show full provenance for one node: source, original write timestamp, every edge incident, decay-adjusted weight, persona tag at write time if available | M | T1.3 | — |
| T1.5 Implement `oc awareness forget <id>` — soft-delete by writing a `supersedes` edge from a tombstone node, OR hard-delete with `--hard`. Identity-kind requires `--confirm` | M | T1.3 | API stability: `forget` semantics enter user-facing API; lock the flag spelling |
| T1.6 Implement `oc awareness correct <id> <new-value>` — writes a `supersedes` edge from the new-valued node to the old; ranker prefers `supersedes` target. Identity-kind requires `--confirm` | M | T1.5 | — |
| T1.7 New CLI module `opencomputer/cli_awareness.py` (mirrors `cli_consent.py` style); wire into `oc` Click group | S | T1.3, T1.4, T1.5, T1.6 | — |
| T1.8 Tests: `tests/cli/test_awareness_review.py` (golden output), `test_awareness_forget.py` (soft + hard + identity-confirm path), `test_awareness_correct.py` (supersedes-edge written) | M | T1.7 | — |
| T1.9 Docs: `docs/awareness/cli.md` covering all four subcommands with examples; link from README features section | S | T1.7 | — |

Milestone-1 total: ~**L** (target: 8–10 working days). Heavier than B + C combined because it ships the user-facing surface first.

#### Milestone 2 — Writer cleanup (stop the bleeding at source)

Done when: every core writer of user-model nodes respects a typed taxonomy (cron heartbeats can't be preferences, session-shape can't be goals); the top 80% of currently-mis-classified rows are migrated to correct kinds (or marked `needs_review`); audit log shows zero new mis-classifications in a one-week observation window.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T2.1 Design `NodeKindValidator` in `plugin_sdk/user_model.py` — per-kind rules (e.g. `preference` values must match the pattern `<dimension>: <value>`; `goal` values can't start with `session goal:`). Strict for core writers; warn-only for plugin writers (configurable) | M | T1.2 | API stability: validator interface enters `plugin_sdk/`; lock the rule shape |
| T2.2 Update `user_model/importer.py` to call validator before `insert_node`; rejections log to `audit.db` table `user_model_writer_rejections` with reason | S | T2.1 | — |
| T2.3 Walk every offender from T1.2 list — fix at source. Each offender is a separate small task; size depends on count from T1.2 | M–L | T1.2, T2.1 | Honest size = the T1.2 list. If 3 offenders, M; if 10+, L |
| T2.4 Migration script `oc awareness migrate` — for each existing node, run the validator; if invalid and value matches a known fixable pattern (e.g. "ambient-sensors prefers ..."), reclassify to the correct kind. Unfixable rows get a `needs_review` metadata flag, surfaced by `oc awareness review --needs-review` | M | T2.1, T2.3 | False reclassification — better to mark for review than auto-fix wrong |
| T2.5 Observation hook: log every node write for one week to `audit.db`; cron checks for new validator failures and notifies `PushNotification` | S | T2.2 | Time-bound; not blocking |
| T2.6 Tests: `tests/user_model/test_validator.py` (per-kind rules), `tests/user_model/test_importer_rejection.py`, `tests/cli/test_awareness_migrate.py` (golden migration plan) | M | T2.4 | — |
| T2.7 Docs: append validator rules + migration story to `docs/awareness/cli.md` | S | T2.6 | — |

Milestone-2 total: ~**M-L** (target: 6–10 working days, depending on T1.2 offender count).

#### Milestone 3 — Context-aware reranker

Done when: `build_user_facts` uses a reranker that combines (kind priority, confidence, decay-adjusted recency_weight, BM25 score vs. current session messages); reranker output cached per `session_id`; `oc awareness explain --session` shows the score breakdown for the current top-K; reranker p99 latency ≤50ms.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T3.1 Implement `opencomputer/user_model/reranker.py` (~250 LOC). One class `UserFactsReranker` with method `score(nodes, session_context) -> list[(node, score, breakdown)]`. Score = `w_kind * kind_priority + w_conf * confidence + w_recency * recency_weight + w_bm25 * bm25_score`. Weights configurable in `<profile>/config.yaml`; sane defaults | M | M1 done | Hyperparameters: defaults will be wrong; tune via T3.5 |
| T3.2 BM25 implementation: pure-Python over node-value tokens vs. last N=20 user messages. Tokenize with simple regex; lowercase; drop stopwords (small built-in list). No external dep | M | T3.1 | Tokenization edge cases (URLs, code identifiers); accept lossy v1 |
| T3.3 Session-context accessor: read `RuntimeContext.persona_tag`, last N user messages from `SessionDB`, foreground app from `awareness/personas/_foreground.py`. Cron / non-session paths fall back to "context-free mode" (skip BM25 term) | S | T3.1 | — |
| T3.4 Wire reranker into `prompt_builder.build_user_facts` — replace the 30-LOC sort with a `UserFactsReranker.score()` call. Cache result per `session_id` in `RuntimeContext`; invalidate on every K-th turn (configurable, default K=5) | S | T3.1, T3.3 | Caching invalidation; keep simple, count-based |
| T3.5 Tuning harness: `oc awareness eval-ranker` — replays last 50 sessions, shows what the reranker would have shown vs. what it actually showed, lets the user score "did this fact help?" thumbs-up/down; weights auto-adjust via simple gradient | M | T3.4 | Time-bound to one afternoon of tuning, not infinite |
| T3.6 `oc awareness explain --session` extension — show per-fact score breakdown for current session's top-K | S | T3.4 | — |
| T3.7 Tests: `tests/user_model/test_reranker.py` (each scoring term in isolation, then combined), `test_reranker_bm25.py` (tokenization + scoring), `test_prompt_builder_with_reranker.py` (integration) | M | T3.4 | — |
| T3.8 Docs: `docs/awareness/reranker.md` covering weights, tuning, observability | S | T3.4 | — |

Milestone-3 total: ~**M-L** (target: 7–9 working days).

#### Milestone 4 — Decay + drift wired into reranker (small, high-value)

Done when: reranker reads `recency_weight` from the edges store (decay engine writes them already; just wire the read); drift-detected nodes (incoming `contradicts` edges from higher-confidence sources) get demoted; `oc awareness explain` shows both as score components.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T4.1 Add `recency_weight_aggregate` accessor: for each node, average `recency_weight` of incident edges; expose via `UserModelStore.node_recency_score(node_id)` | S | M3 done | — |
| T4.2 Add `drift_penalty` accessor: count of `contradicts` edges pointing to the node weighted by source confidence; expose via `UserModelStore.node_drift_score(node_id)` | S | T4.1 | — |
| T4.3 Update reranker scoring to consume both. New term: `w_drift * (1 - drift_score)` | S | T3.4, T4.1, T4.2 | Weight tuning — start with default 0.0 (opt-in), enable after one week of observation |
| T4.4 Confirm decay scheduler runs on the deployment box. If it's only a CLI command, add `oc decay tick` to the existing `oc cron` configs OR document the cron entry the user must add | S | T4.1 | Forgotten cron = stale weights forever |
| T4.5 Tests: integration test that ages an edge, re-runs reranker, asserts demotion | S | T4.3 | — |

Milestone-4 total: ~**S-M** (target: 3–4 working days).

#### Milestone 5 — Observability + docs (REQUIRED to declare done)

Done when: a user looking at the top-of-prompt user-facts block can answer "why am I seeing this fact?" in two CLI commands.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T5.1 Polish `oc awareness explain --session` output: pretty table with score breakdown columns | S | M3 done | — |
| T5.2 `oc awareness debug` — dump `(session_id, persona_tag, foreground_app, last_5_message_tokens, top_K_facts, scoring_breakdown)` as JSON for a bug report | S | M3 done | — |
| T5.3 Top-level `docs/awareness/README.md` with architecture diagram + pointers to the three sub-docs | S | M1–M4 done | — |
| T5.4 Update `README.md` features section with the new `oc awareness` subcommands | S | T5.3 | — |

Milestone-5 total: ~**S** (target: 2 working days).

### Milestone summary

| # | Milestone | Size | Calendar (1 eng) |
|---|---|---|---|
| **1** | **Audit + correction CLI (MVP)** | L | 8–10 days |
| 2 | Writer cleanup (stop bleeding at source) | M-L | 6–10 days |
| 3 | Context-aware reranker | M-L | 7–9 days |
| 4 | Decay + drift wired into reranker | S-M | 3–4 days |
| 5 | Observability + docs | S | 2 days |

Total: **~5–6 calendar weeks** for one engineer working sequentially. Parallelizable to ~4 weeks if M2 (writer cleanup) and M3 (reranker) run concurrently — they don't share files.

### Explicitly out of scope (v1)

- **Persona / life-events behaviour growth (Approach E).** Highest-upside item but gated on clean data; defer to v2.
- **Embedding-based reranker.** BM25 is enough for v1.
- **`oc awareness import/export`** for cross-machine sync.
- **GUI for graph editing.** CLI-only in v1.
- **LLM-scored facts** (Approach F). Loses cross-session compounding.
- **Real graph DB** (Approach G). No new query OC needs.
- **Per-tool reranker modes.** YAGNI'd.

---

## Phase 4 — /audit-plan

Harsh critic pass. Revising until it holds.

### 4.1 — Unvalidated assumptions

| Assumption | Validation status | Plan revision |
|---|---|---|
| Decay scheduler is actually running in production | **Unvalidated.** Code exists; cron entry may not. | T4.4 added to verify; if not running, this is a 5-min fix. |
| BM25 over short node-values vs. short message contexts produces non-noise scores | **Plausible but unvalidated.** Both sides are short text; BM25 might collapse to "does any word match." | T3.5 tuning harness validates with real sessions; if BM25 is noise, fall back to keyword-overlap or skip the term. |
| The 30-LOC `build_user_facts` is the only consumer of user-fact rendering | **Unvalidated.** Could be other paths. | T1.1 explicitly maps every consumer of `<user-facts>` in `base.j2` and any other template. |
| Mis-classified nodes can be reclassified by value-pattern matching alone | **Unvalidated.** "ambient-sensors prefers Wednesday 14:00" is fixable; arbitrary user-typed prefs may not be. | T2.4 accepts `needs_review` flag for unresolvable rows; doesn't auto-fix everything. |
| User correction CLI semantics ("forget" = soft-delete via supersedes; "--hard" for full deletion) match user mental model | **Unvalidated.** | Test with self for one week before locking; T1.5 commits to soft-delete default, hard is opt-in. |

### 4.2 — Undersized tasks hiding real complexity

- **T1.5 `forget` is more than M.** Soft-delete via supersedes-edge needs a tombstone-node concept the schema doesn't formalize. Either invent a `kind="tombstone"` node type (schema change, painful) or use a `metadata.deleted=true` flag (works, slightly hacky). Resize: **M is still right with the metadata-flag approach; document the choice.**
- **T2.3 "Walk every offender and fix at source"** is dependent on T1.2's count. If T1.2 returns 10 writers, T2.3 is 2 weeks alone. **Hard exit-criterion:** fix the top 80% by row count; document the rest as known-bad and accept.
- **T3.2 BM25 with proper tokenization** for code-heavy node values (paths, identifiers, snake_case) is non-trivial. Resize: **M is right but expect 1 extra day for tokenization tweaks.**
- **T3.5 tuning harness** is open-ended by nature. **Time-bound to one afternoon, then commit to whatever weights it produced.** Don't chase perfect.
- **T4.4 decay-cron verification** could uncover a real "decay isn't running" problem that adds a day. Accept.

After resizing: M1 stays L (8–10 days), M2 stays M-L (6–10 days), M3 stays M-L (7–9 days). **Total still ~5–6 weeks.**

### 4.3 — What breaks if Milestone 1 slips

M1 ships the inspection CLI. If it slips:

- **M2, M3, M4 are blocked** in a soft sense — you can run them without the CLI but you can't validate them. Bug reports become "the facts look weird" with no way to inspect why.
- **You can't tune M3 without M1's `explain`.** T3.5 depends on `oc awareness explain --session` from M1.
- **Mitigation:** ship `oc awareness review` (T1.3) as a single-PR fast-track — that alone gives you 60% of the inspection value. The rest of M1 can slip and the other milestones still proceed.

**Verdict:** M1 is the right MVP because it's the gate for validating everything else, not because it has the most code.

### 4.4 — Simpler path to the same outcome?

Considered: skip M2 (writer cleanup) entirely; let M3 reranker handle the noise via aggressive BM25 scoring.

**Rejected.** BM25 will down-rank irrelevant rows for *this* session, but they still inject for sessions where the BM25 score happens to favour them. Junk-at-source compounds; cleaning at source stops the compounding.

Considered: skip M4 (decay/drift wiring) since decay already writes weights — eventually the reranker will read them when refactored.

**Rejected as a v1 cut, kept in plan.** T4.1–T4.3 is only 3 days and turns a dormant subsystem into a live one. High value/effort ratio.

Considered: collapse M5 (docs) into the other milestones.

**Partially accepted.** Each milestone's per-milestone docs task (T1.9, T2.7, T3.8) already covers per-feature docs. M5 is only the top-level README + cross-cutting `docs/awareness/README.md`. Keep but trim to ~2 days.

### 4.5 — What will I wish I'd done differently in the retro?

Pre-emptive retro hypotheses:

1. **"I should have shipped just `oc awareness review` first as a 1-day spike."** → Already addressed in §4.3 mitigation. Ship T1.3 alone if needed.
2. **"BM25 wasn't enough; we needed embeddings."** → Acceptable v2 work. v1 ships and we learn from real use.
3. **"The writer cleanup discovered more offenders than expected and bloated M2."** → Mitigated by T2.3's 80% exit-criterion. Don't chase the long tail.
4. **"Decay was actually broken and we discovered it during T4.4."** → That's a feature, not a bug. Decay running silently broken for months is worse than discovering it now.
5. **"`forget` semantics confused users."** → T1.5 ships soft-delete default + `--hard`. Document with examples. Reserve right to change behaviour in v2 if needed; the CLI flag spelling stays stable.

All five mitigations folded into the task list above.

### 4.6 — Revised plan summary

The plan that ships, after the audit:

1. **Milestone 1 (MVP):** Audit + correction CLI (`oc awareness review/forget/correct/explain`). **8–10 days.** Includes mandatory pre-task source-read of the existing user_model and a count of writer offenders.

2. **Milestone 2:** Writer cleanup. Validator at the SDK layer, called from `importer.py`. Fix top 80% of mis-classifying writers; mark the rest. **6–10 days** depending on offender count.

3. **Milestone 3:** Context-aware reranker. BM25 over session messages vs. node values, weighted with kind + confidence + recency. **7–9 days.** Tuning harness time-bound to one afternoon.

4. **Milestone 4:** Wire decay + drift into the reranker as new scoring terms. **3–4 days.** Includes verifying the decay scheduler is actually running.

5. **Milestone 5:** Top-level docs + README updates. **2 days.**

**Calendar:** ~5–6 weeks sequential, ~4 weeks if M2 and M3 run concurrently (no shared files).

**Explicit deferrals** (no code in v1): persona/life-events behaviour growth (Approach E), embedding-based reranker, import/export CLI, GUI editor, LLM-scored facts, graph DB migration, per-tool reranker modes.

### 4.7 — Pre-flight checklist before any code

Before T1.1:

- [ ] Confirm `pytest opencomputer/user_model/` is green on `main` (baseline).
- [ ] Confirm `ruff check opencomputer/user_model/ opencomputer/awareness/` is clean.
- [ ] Confirm decay scheduler is configured in `<profile>/config.yaml` cron or `oc cron list` (if not, M4 grows by 1 day).
- [ ] Confirm the parity plan (PART-1/PART-2 in `2026-05-16-oc-parity-with-hermes-openclaw/`) has shipped M1–M5 — this awareness work is **post-parity-plan**, not concurrent.
- [ ] Confirm the working profile's `user_model/` directory exists (`ls ~/.opencomputer/<profile>/user_model/graph.sqlite`); if not, run `oc profile bootstrap` first.

If any of these fail, halt and report; don't paper over.

---

## Honest closing note

This plan deliberately defers the most exciting item — Approach E, making persona and life-events produce visible behaviour changes. It's the highest-upside item on the list. It's deferred because it needs clean data to not actively misbehave, and clean data is what M1–M4 produce.

If you want to swap something in (e.g. ship a basic E in v1 instead of M4), I'm happy to make that trade. But the total budget *is* fixed at ~5–6 weeks for the listed scope. Adding E means cutting something else; the obvious cut is M4 (decay+drift wiring), which then ships in v2 alongside the embedding-based reranker.

**Recommendation:** ship as planned. Once you have clean data, a working reranker, and visible inspection, *then* the E work becomes high-confidence rather than high-risk.
