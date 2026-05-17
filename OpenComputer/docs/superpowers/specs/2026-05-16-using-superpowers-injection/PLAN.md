# `using-superpowers` always-injection — Plan

Date: 2026-05-16
Owner: Saksham
Working dir: `/Users/saksham/Vscode/claude/OpenComputer`
Scope: single-PR, post-parity-plan, post-awareness-cleanup (does not block either).

---

## Pre-work: What's actually in the codebase (verified, not guessed)

Greped on-disk, not assumed:

| Component | Status in OC | Path / evidence |
|---|---|---|
| `Skill` tool class | **Shipped, registered.** | `opencomputer/tools/skill.py:54`; registered at `opencomputer/cli.py:653` via `registry.register(SkillTool())` |
| Skill list rendered into system prompt | **Shipped.** Every system prompt loops over all 163 skills and renders `- name — description`. | `opencomputer/agent/prompts/base.j2:230-247` (Slot 4) |
| `using-superpowers/SKILL.md` body | **Exists on disk.** Contains the 1%-rule body, SUBAGENT-STOP guard, priority order, the flow diagram. | `opencomputer/skills/using-superpowers/SKILL.md` |
| `using-superpowers` body auto-injected into the prompt | **NOT shipped.** Greping `opencomputer/agent`, `opencomputer/skills_hub`, `opencomputer/tools`, `plugin_sdk`: zero hits for `using-superpowers` / `using_superpowers` outside the SKILL.md file itself. | (verified gap) |
| Prompt slot for always-on skill body | **Does not exist.** Slots 1–7 in `base.j2` cover SOUL.md, tool guidance, memory, skills list (descriptions only), workspace context, pinned files, timestamp, persona. No slot loads a skill body. | `base.j2:14-286` |
| `DynamicInjectionProvider` machinery | **Shipped.** General-purpose system for adding cross-cutting injection blocks per turn. | `opencomputer/agent/injection.py`; `plugin_sdk/injection.py` |

**The gap is concrete and small:** the skill body that establishes the standing 1%-rule discipline is on disk as a SKILL.md file that the model only sees if it first calls `Skill(name="using-superpowers")`. There is no code path in OC that auto-loads it before the first user message. Claude Code does this; OC does not.

**Effect on agent behaviour:** the model sees the menu of 163 skill descriptions every turn, but never sees the "you MUST invoke skills when ≥1% chance they apply" instruction unless it independently decides to invoke `using-superpowers`. Which it won't, because it doesn't know it should. Chicken-and-egg.

**Evidence from this very session:** the conversation that produced this plan, with the entire 163-skill list visible in context, included multiple points where the model (me) should have invoked skills (`writing-plans`, `silent-failure-hunter`, `verification-before-completion`) and did not. The mechanism is wired; the discipline-forcing rule is silent.

---

## Phase 1 — /brainstorm

### Goal

Make `using-superpowers` (or its load-bearing rules) reach the model on every turn so the 1%-rule is an actual standing instruction, not a dormant SKILL.md file. Keep prompt-token cost bounded. Don't break existing skill resolution.

### Approaches considered

#### Approach A — "Inline the body into `base.j2`"

Hardcode the body of `using-superpowers/SKILL.md` into a new slot in `base.j2`. ~50 lines of Jinja-escaped markdown, rendered every turn.

- **Effort:** XS (1 hour).
- **Risk:** Medium. If the SKILL.md body and the inlined copy drift apart, behaviour diverges between "invoked via Skill tool" and "always-injected." Two sources of truth.
- **Upside:** Simplest possible delivery. Always-on by definition.
- **Downside:** Maintenance burden. Skill author updates `using-superpowers/SKILL.md` and forgets the inlined copy → silent skew.

#### Approach B — "New Slot 0 in `base.j2` that file-reads the SKILL.md body at template-render time"

Add a Jinja slot that does `{% include 'skill_body:using-superpowers' %}` or pulls the rendered body from a context var. The template renderer reads `using-superpowers/SKILL.md`, strips frontmatter, injects the body into the prompt. One source of truth (the SKILL.md file); the prompt always reflects current content.

- **Effort:** S (3-4 hours including tests).
- **Risk:** Low. Reads file at prompt-build time, same lifecycle as every other slot.
- **Upside:** Single source of truth. Skill author updates SKILL.md; next turn reflects it. No drift.
- **Downside:** Prompt-build time gains an unconditional file read. Cost is microseconds (the file is ~3 KB). Negligible.

#### Approach C — "DynamicInjectionProvider for using-superpowers"

OC already has `DynamicInjectionProvider` (`agent/injection.py`). Build a provider that loads `using-superpowers/SKILL.md` body once at agent-loop init, caches it, returns it on every `collect_all()`. Registers via the existing `register_injection_provider` API.

- **Effort:** S (1 day with tests).
- **Risk:** Low-medium. Adds one more registration step at startup. Provider order matters for prompt-cache stability; needs explicit priority value.
- **Upside:** Uses the abstraction OC already built for exactly this case ("cross-cutting modes inject system reminders without scattering `if` checks" — per AGENTS.md).
- **Downside:** More machinery for a one-line conceptual "always inject this file." Two abstractions where one slot would suffice.

#### Approach D — "Auto-invoke `Skill(name='using-superpowers')` as a first-turn synthetic tool call"

On the very first turn of each session, the agent loop synthesizes a Skill tool_use + tool_result pair so the model sees the body as a tool result (same shape as a normal invocation). After turn 0, body is in conversation history.

- **Effort:** M (2 days). Touches `loop.py` first-turn path; needs care around session resume (don't double-inject when resuming a session that already has it).
- **Risk:** Medium. Bloats every session's persisted history by ~3 KB. Edge cases: cron context (no session), `oneshot` runs, batch runner, subagent spawns (`<SUBAGENT-STOP>` guard in the skill body handles this — but the body has to reach the subagent first for that to fire).
- **Upside:** Body lives in conversation history → survives compaction. Same path as a normal Skill invocation → no special-case handling downstream.
- **Downside:** Persisted history bloat × every session × every profile. Adds up.

#### Approach E — "Add a hard-coded 'standing instructions' block to `base.j2` Slot 2 (tool-aware behavior guidance)"

Don't inline the whole `using-superpowers/SKILL.md`. Just lift the load-bearing 6 lines (the 1%-rule core) and add them to the existing tool-guidance slot. Reference the full skill: "See `using-superpowers` SKILL.md via the Skill tool for the full priority order and instruction precedence."

- **Effort:** XS (30 min).
- **Risk:** Low. Editing an existing slot, not adding infrastructure.
- **Upside:** Smallest prompt-token cost. The model gets the critical rule (1%-rule); the rest of the skill body remains lazy-loadable.
- **Downside:** The skill body has subtle pieces (Instruction Priority order, SUBAGENT-STOP guard, platform-adaptation notes) that the lifted 6 lines won't cover. Model sees the rule but not the full procedure.

#### Approach F — "Do nothing; add the rule to MEMORY.md instead"

Use OC's MEMORY.md (declarative memory, always-in-prompt) to hold the 1%-rule. Update the agent's MEMORY.md to include a "Behavioral rule: invoke skills before responding" entry.

- **Effort:** XS (15 min).
- **Risk:** High. MEMORY.md is profile-specific and user-editable; a stock OC install doesn't ship with this rule pre-populated. New users wouldn't get it. Also: MEMORY.md is for *user-specific* facts and behavioural rules learned from interactions, not for system defaults.
- **Upside:** Zero code change.
- **Downside:** Wrong abstraction. Conflates "system defaults shipped with OC" with "user-specific memory."

#### Approach G — "Make `using-superpowers` discoverable by upgrading the existing skills-list block in `base.j2`"

Don't add a new slot. Modify the existing skill-list rendering loop so it *unconditionally* expands the body of any skill whose frontmatter declares `always_on: true`. The skill author opts in via frontmatter; `using-superpowers/SKILL.md` gains an `always_on: true` field; the renderer reads the body and injects it inline.

- **Effort:** S–M (1 day with tests + a `Slot 4b` in the template).
- **Risk:** Low. Schema is additive (existing skills with no `always_on` field stay description-only).
- **Upside:** Generalises beyond `using-superpowers`. Future skills (e.g. a custom "always read CLAUDE.md first" skill) can opt in via the same frontmatter flag. Single source of truth.
- **Downside:** A frontmatter flag that affects prompt rendering is a v1 API commitment for plugin authors.

#### Approach H — "Inject only on plan-mode / specific contexts"

The 1%-rule is most valuable in deep work (writing plans, fixing bugs). Conditionally inject the `using-superpowers` body only when `plan_mode=True` or when the persona classifier sees an engineering context.

- **Effort:** M (2 days; needs plumbing into persona / runtime context).
- **Risk:** Medium-high. Behaviour now context-dependent — sometimes the model has the rule, sometimes not. Same code path produces different behaviours per session, which is a debugging-hazard footgun.
- **Upside:** Lower prompt-token cost in casual chat.
- **Downside:** Inconsistency is worse than the cost it saves. A standing instruction that only stands sometimes is not a standing instruction.

### Scoring

| Approach | Effort | Risk | Upside | Verdict |
|---|---|---|---|---|
| A — Inline body into `base.j2` | XS | Medium (drift) | High | Drift risk kills it |
| B — Slot reads SKILL.md at render | S | Low | High | Strong candidate |
| C — DynamicInjectionProvider | S | Low-Medium | Medium | More machinery than needed |
| D — Synthetic first-turn Skill call | M | Medium | Medium | History bloat × every session |
| E — Lift 6 lines into Slot 2 | XS | Low | Medium-Low (partial) | Cheapest but incomplete |
| F — MEMORY.md hack | XS | High | Low | Wrong abstraction |
| G — `always_on:` frontmatter flag | S–M | Low | High (extensible) | Strong candidate; bigger commit |
| H — Conditional injection | M | Medium-high | Low-Medium | Standing-instruction self-contradiction |

### Convergence

Top 2: **B and G.**

- **B** is the smallest-surface, single-file change that solves the immediate problem. Always-on body for one skill, no API commitments.
- **G** generalises B with a frontmatter flag, paying ~1 extra day to enable any future skill to opt into the same behaviour. Cleaner architecture, real API commitment.

### Winner: **G — `always_on:` frontmatter flag**

Why G beats B on merit (not familiarity):

- **B solves the immediate problem.** G solves the *category*. The next time someone asks "make this skill always-on," G is a 2-line SKILL.md edit; B is another `base.j2` slot.
- **G fits OC's existing extensibility patterns.** Skill frontmatter already supports `context: fork`, `tools:`, `model:`, `agent:`, `isolation:`, `paths:`, `disable_model_invocation`, `user_invocable`, `argument_hint`, `skill_model` — adding `always_on: bool = False` is consistent with that pattern. B is an ad-hoc one-off.
- **API commitment is bounded.** The frontmatter schema is already a plugin-author contract. Adding one additive optional field with `False` default doesn't break any existing skill.
- **Both Hermes and Claude Code lack this generalization.** OC ships a small-but-real improvement on the pattern, rather than copying claude-code's special-case handling of one specific skill name.

The losing item I'd pay attention to: **G's downside is "frontmatter flag that affects prompt rendering is a v1 API commitment."** Mitigated by keeping the schema strictly additive in v1 (only the `always_on: bool` field), with the rendering logic isolated to one new template block.

---

## Phase 2 — /audit-design

Stress-testing Approach G. Each finding is resolved or accepted-risk.

### 1 — Assumption check

| Assertion | Validated? | Resolution |
|---|---|---|
| Skill frontmatter parser is extensible (additive fields don't break loaders) | **Likely yes** — `memory.py:388-410` shows existing fields parsed with defaults, suggesting a tolerant loader. | T1.1 reads the parser end-to-end before adding the field. If it's strict-schema, fall back to a `paths: ["**"]` hack (which already exists for "always-active") or escalate. |
| Prompt cache stability — adding a Slot 4b that includes skill body content won't invalidate the prompt cache mid-session | **Unvalidated.** Provider-level cache keys may hash the system prompt; adding content invalidates it on the user's *next* turn for sure. Then it stays cached. | Accept-risk: one-time cache invalidation when this lands. Document in the release notes. |
| Only `using-superpowers` opts into `always_on` initially | **By policy, yes.** Plugin authors can opt in later. | Tests cover both "no skill has always_on" (no Slot 4b) and "one skill has always_on" (renders correctly). |
| Body content is safe to inject — no template injection, no XSS-like Jinja escapes | **Unvalidated.** SKILL.md contains markdown; Jinja's `{{ var }}` HTML-escapes by default. If the body has `<` or `{%`, it could mis-render. | T1.2 marks the body as `| safe` after stripping frontmatter; tests assert markdown content (`<EXTREMELY-IMPORTANT>` tag etc.) round-trips. |
| The 1%-rule body actually changes model behaviour | **Empirical, unvalidated.** The body says "you MUST invoke skills"; whether the model obeys is a behavioural test, not a unit test. | Out of scope for the code change. Manual smoke-test post-merge: hand the agent a task with an obvious skill match (e.g. "write a plan for X") and check whether it invokes `writing-plans`. If not, the gap is *also* in the body itself, which is a separate problem. |

### 2 — Architecture stress (edge cases)

- **Two skills both declare `always_on: true`.** Resolution: both render, in deterministic order (by skill `name` alphabetical). Documented.
- **Skill author marks a 50 KB SKILL.md body as `always_on: true`.** Resolution: enforce a cap (e.g. 8 KB body length) at parse time; oversize → reject the `always_on` flag with a warning, fall back to description-only. Prevents prompt-token blowup.
- **Skill body uses Jinja-conflicting syntax** (`{%`, `{{`). Resolution: render with `| safe` filter; bodies are markdown, not Jinja. Tests cover `<EXTREMELY-IMPORTANT>` HTML-ish tags.
- **`using-superpowers/SKILL.md` removed/renamed in a future release.** Resolution: the skill-list loop already handles missing skills (renders empty). Slot 4b checks if `always_on_skills` is empty before emitting the header.
- **Subagent context.** `using-superpowers/SKILL.md` has a `<SUBAGENT-STOP>` guard at the top: "If you were dispatched as a subagent to execute a specific task, skip this skill." The body still injects in subagent prompts; the guard tells the model to ignore it. Composability holds because the guard is in the body, not the renderer.
- **Cron context (no session, no user).** Always-on body still injects. Probably fine — cron jobs running scripted skills don't read the body anyway. Worst case: 3 KB of wasted tokens per cron run. Acceptable for v1; could add a `cron_skip: true` field later.

### 3 — Alternative dismissal

Approaches A, F, H dismissed on merit:
- **A** (inline body) has drift risk vs. G's single source of truth.
- **F** (MEMORY.md) is wrong abstraction.
- **H** (conditional injection) contradicts "standing instruction."

Approach D (synthetic first-turn) dismissed because of session-history bloat — every session in the SessionDB grows by 3 KB forever, multiplied by 1000s of sessions over time. Real cost.

Approach E (lift 6 lines into Slot 2) is a viable smaller-scope fallback if G hits unexpected schema-parser issues. Documented as a Plan-B option.

Approach C (DynamicInjectionProvider) is technically cleaner from a "use the right abstraction" standpoint but pays infrastructure cost (provider registration, ordering, async collect) that B/G don't need. Documented as a Plan-C if extensibility outgrows G's frontmatter flag.

### 4 — Requirement gap

- **The user wants the standing instruction to actually stand.** Implicit: the model should *behave differently* after this lands. Resolution: smoke-test included as a verification step in the plan.
- **Implicit: don't break existing skill loading.** Resolution: `always_on` is additive optional with `False` default; existing skills load identically.
- **Implicit: don't blow up token budgets.** Resolution: 8 KB cap per always-on body; current `using-superpowers/SKILL.md` is ~3 KB so fits comfortably.
- **Implicit: prompt cache stability.** Resolution: one-time invalidation accepted; subsequent turns stable.

### 5 — Composability

- **`always_on:` + `paths:` (cwd-gated):** if both set, which wins? Resolution: paths wins. `always_on` means "always when the skill would otherwise be in the list" — paths already gates list inclusion. So a `paths`-gated skill is only always-on when its path matches. Documented.
- **`always_on:` + `context: fork`:** can a skill be both always-injected AND fork-only when invoked? Resolution: yes, they're orthogonal. `always_on` controls prompt injection; `context: fork` controls invocation behaviour. Tests cover both.
- **`always_on:` + `disable_model_invocation: true`:** a human-only-invocable skill that's always-injected? Use case: a personal reminder. Resolution: allowed. The body renders; the model just can't invoke the skill via tool.
- **`always_on:` skill + the existing 163-skill description loop:** does the body appear twice (in Slot 4 list AND Slot 4b body)? Resolution: yes, both. The description in the list tells the model the skill exists; the body in Slot 4b is the standing content. Designed-in redundancy.

### 6 — Scope honesty

Where am I undersizing?

- **"Frontmatter flag" sounds tiny.** Plumbing it through the skill loader, the path matcher, the prompt builder, and the renderer is 4 integration points, not 1. Honest size: **S (1 day) is still right** but includes 4 integration sites.
- **Tests.** Need: parser test (additive field with default), renderer test (slot 4b appears/absent), body cap test (oversize rejected), subagent test (body injects, guard respected), composability tests (always_on + paths, + fork, + disable_model_invocation). Honest size: **6-8 tests, 200 LOC.**
- **Docs.** Update `docs/plugin-authors.md` and add a section to skill authoring docs. Honest size: **S (half-day).**

Total: **1.5 days** for one engineer.

### 7 — API stability

- New: `SkillFrontmatter.always_on: bool = False`. **This is a v1 API commitment.** Additive, defaults to False; existing plugins unaffected. Removal would be a major break; renaming is bounded.
- New: Slot 4b in `base.j2`. **Internal to the renderer** — not part of `plugin_sdk/`. Free to refactor.
- New: 8 KB body cap. **Documented constraint.** Bumping later is safe; lowering later breaks bodies that grew. Set the cap generously initially (16 KB?) so we don't have to raise it.

### 8 — Failure map

| Choice | Production failure | Mitigation |
|---|---|---|
| `always_on: true` + 16 KB body | Prompt-token bloat per turn | Hard cap with reject-and-warn at parse time |
| Two skills both `always_on: true` | Both inject; order matters for prompt cache | Deterministic alphabetical order documented |
| Skill body contains Jinja syntax | Template render error | Body marked `| safe` after frontmatter strip; tests cover this |
| `using-superpowers/SKILL.md` deleted | Slot 4b silently empty | Renderer handles empty list gracefully; release notes warn about deleting opt-in skills |
| Cron / batch / subagent paths | Body injects unconditionally | `<SUBAGENT-STOP>` guard already in body; cron cost accepted |
| Provider prompt cache invalidation | One-time cache miss on upgrade | Release notes flag this; expected behaviour for any system-prompt change |

### 9 — YAGNI sweep

- **Per-skill `always_on_priority` field for ordering.** No. Alphabetical is deterministic and ships fewer surfaces.
- **Conditional `always_on` (e.g. `always_on_when: plan_mode`).** No. That's Approach H rejected; standing means standing.
- **GUI for toggling `always_on` per skill.** No. Frontmatter edit is the interface.
- **`always_on` for plugin-bundled skills vs. user-authored skills (different precedence).** No. Same treatment for both.
- **Auto-injecting `using-superpowers` if `always_on` doesn't exist in its frontmatter** (special-case fallback). No. Migration step: add `always_on: true` to `using-superpowers/SKILL.md` as part of this PR. No special cases in code.

---

## Phase 3 — /plan

### "Done" in one sentence

A skill's `always_on: true` frontmatter flag causes its (frontmatter-stripped, capped-length) body to render in every system prompt unconditionally; `using-superpowers/SKILL.md` opts in via this flag; the model receives the 1%-rule body in slot 4b of `base.j2` on every turn; tests cover the parser, renderer, body cap, and composability with `paths` / `context: fork` / `disable_model_invocation`; release notes flag the one-time prompt-cache invalidation.

### Milestones

#### Milestone 1 — Schema + parser support for `always_on` (LOAD-BEARING)

Done when: `SkillFrontmatter.always_on: bool = False` parses cleanly; existing skills (with no `always_on` field) load as before; oversized bodies (>16 KB) on `always_on` skills are rejected at parse time with a clear warning.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T1.1 Read `opencomputer/agent/memory.py:388-410` + the skill frontmatter dataclass end-to-end; document current schema in `docs/refs/oc-skill-frontmatter-baseline.md` | S | — | If parser is strict-schema (rejects unknown fields), need a different approach; gate continuation on this |
| T1.2 Add `always_on: bool = False` field to the SkillFrontmatter dataclass | XS | T1.1 | API commitment; lock the field name now |
| T1.3 Add 16 KB body cap; reject `always_on=true` with body >16 KB at parse time, warn to `audit.db` | S | T1.2 | Decide cap value; 16 KB chosen as ~4× current `using-superpowers` size |
| T1.4 Unit tests: parser accepts `always_on: true/false/missing`; cap enforcement; non-`always_on` skill with huge body is fine | S | T1.2, T1.3 | — |

Milestone-1 total: **S (1 day)**.

#### Milestone 2 — Renderer: Slot 4b in `base.j2` (**MVP**)

Done when: `base.j2` has a new Slot 4b that renders, after the existing skill-list Slot 4, the body of every skill where `always_on: true`. Bodies appear in deterministic alphabetical order. When no skill has `always_on: true`, Slot 4b is fully omitted (no orphan header, no whitespace cruft).

| Task | Size | Deps | Risks |
|---|---|---|---|
| T2.1 Update `prompt_builder.py::build_skills_block` (or equivalent — find via grep) to also collect `always_on_skills` list and expose to template context | S | M1 done | API to template context is per-builder convention; match it |
| T2.2 Add Slot 4b to `opencomputer/agent/prompts/base.j2` after Slot 4 (line 247); render with `| safe` after stripping frontmatter | S | T2.1 | Jinja escape edge cases — covered by T2.4 |
| T2.3 Add header text in Slot 4b ("## Standing skill instructions — these are always active") so the model can distinguish the body from list descriptions | XS | T2.2 | — |
| T2.4 Tests: Slot 4b renders for `always_on=true` skill; empty when no skill opts in; Jinja-conflicting content (`<EXTREMELY-IMPORTANT>` tag, `{` chars) round-trips correctly | M | T2.2 | — |
| T2.5 Mark `using-superpowers/SKILL.md` with `always_on: true` in its frontmatter | XS | T2.2 | This is the MIGRATION — flips the standing rule on at the same moment the code lands |
| T2.6 Smoke test: launch `oc chat`, send "make a plan for X", verify model invokes `Skill(name="writing-plans")` (or comparable). Manual, not automated. | S | T2.5 | Behavioural; not gating CI but gating "is this actually working?" |

Milestone-2 total: **S-M (1.5 days)**. **This is the MVP** — landing it lights up the 1%-rule.

#### Milestone 3 — Composability tests + docs

Done when: tests cover `always_on` interacting with `paths` (cwd-gated), `context: fork`, `disable_model_invocation`, and `user_invocable: false`; `docs/plugin-authors.md` and skill-authoring docs explain the new field with examples.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T3.1 Tests: `always_on=true` + `paths=["/path/that/doesnt/match"]` → body NOT injected | S | M2 done | — |
| T3.2 Tests: `always_on=true` + `context: fork` → body still injects (forking only applies on invocation, not on prompt presence) | S | M2 done | — |
| T3.3 Tests: `always_on=true` + `disable_model_invocation: true` → body injects, model can't invoke via tool | S | M2 done | — |
| T3.4 Update `docs/plugin-authors.md` with `always_on` field + size cap + composability matrix | S | M2 done | — |
| T3.5 Add example skill to `examples/` (or wherever plugin examples live) demonstrating `always_on: true` | XS | T3.4 | — |

Milestone-3 total: **S (0.5-1 day)**.

#### Milestone 4 — Release notes + handoff

Done when: `CHANGELOG.md` documents the prompt-cache invalidation; an entry in `MEMORY.md` (or wherever OC notes behavioural changes) flags that the agent now respects the 1%-rule from turn 1.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T4.1 CHANGELOG.md entry under next release: "skill frontmatter gains `always_on: bool`; `using-superpowers` opts in; one-time provider prompt-cache invalidation expected" | XS | M3 done | — |
| T4.2 Update `docs/superpowers/specs/2026-05-16-SESSION-HANDOFF.md` (the parent handoff) to note this work shipped + remove it from open-items if listed there | XS | T4.1 | Cross-doc consistency |

Milestone-4 total: **XS (1 hour)**.

### Milestone summary

| # | Milestone | Size | Calendar |
|---|---|---|---|
| 1 | Schema + parser support | S | 1 day |
| **2 (MVP)** | **Renderer: Slot 4b + opt-in `using-superpowers`** | S-M | 1.5 days |
| 3 | Composability tests + docs | S | 0.5-1 day |
| 4 | Release notes + handoff | XS | 1 hour |

Total: **~3 working days** for one engineer, sequential.

### Explicitly out of scope (v1)

- `always_on` ordering control (priority field). Alphabetical is enough.
- Conditional `always_on` (per persona, plan-mode, etc.). Approach H rejected.
- GUI / TUI for toggling `always_on`. Frontmatter edit is the interface.
- Auto-prompt-cache invalidation handling (clearing provider caches programmatically). Out of scope; release notes warn.
- More than one skill opting into `always_on` initially. Only `using-superpowers` flips on in this PR. Other skills opt in via their own future PRs.
- Backporting `<SUBAGENT-STOP>` semantics into the renderer (currently the guard is in the body and relies on the model honouring it). Renderer-level subagent skipping is a v2 feature if needed.

---

## Phase 4 — /audit-plan

Harsh critic pass.

### 4.1 — Unvalidated assumptions

| Assumption | Validation | Plan revision |
|---|---|---|
| Skill frontmatter parser accepts additive fields | T1.1 explicitly checks | If parser is strict, escalate; do not silently fail |
| `prompt_builder.py` has a skill-block builder we can extend | Greped in pre-work; needs T2.1 to confirm location | T2.1 starts with a grep; if the builder is in a non-obvious place, document the actual path before editing |
| Markdown body content can render via Jinja `| safe` without security risk | Bodies are markdown, not HTML; Jinja safe-mode is a render-time decision, not a user-input vector. Skills are shipped by OC + plugin authors (trusted), not user-supplied. | Accept-risk: skills are trusted content. |
| 16 KB cap is generous enough for foreseeable always-on skills | `using-superpowers` is ~3 KB; 5× headroom is plenty. | Lock value; raisable later. |
| Behavioural change (model actually invokes skills more) will be observable | Smoke test T2.6 | If smoke test shows no behavioural change, that's a SEPARATE bug in the skill body text or model adherence, not in this code. Documented. |

### 4.2 — Undersized tasks hiding real complexity

- **T2.1 "Update `prompt_builder.py`"** assumes the skill-list builder is a clean isolated function. If it's tangled with caching, persona overlays, or the workspace-context block, this grows. **Size: S → S-M**. Add half-day buffer.
- **T2.5 "Mark `using-superpowers` with `always_on`"** is a one-line frontmatter edit. **Size remains XS** but it's the *load-bearing* migration step — landing the code without flipping `using-superpowers` ships an empty Slot 4b. Make sure T2.5 lands in the same PR as T2.2.
- **T2.6 smoke test** depends on having a working `oc chat` against an actual LLM. If your model account is paused / out of credit / proxy-misconfigured, this blocks. **Treat as gate** — fix the chat environment before declaring done.

### 4.3 — What breaks if Milestone 1 slips

M1 is the schema. If it slips:
- M2 cannot ship — Slot 4b can't read a field that doesn't exist.
- M3, M4 are blocked because they depend on M2.

**Mitigation:** M1 is genuinely small (1 day, one field + cap + tests). If it slips beyond 2 days, the parser is harder than expected → escalate to user.

**Fallback if M1 is impossible:** Approach E (lift 6 lines into Slot 2) ships the 1%-rule core without any frontmatter changes. Half-day fallback path. Documented earlier as Plan-B.

### 4.4 — Simpler path to the same outcome?

Considered: skip the frontmatter flag, just inline the body of `using-superpowers/SKILL.md` directly into `base.j2`.

**Rejected.** That's Approach A. Drift is the issue: the SKILL.md and the inlined copy diverge over time. The frontmatter flag costs ~1 extra day and earns single-source-of-truth + extensibility.

Considered: only inject the 1%-rule line, not the whole body.

**Rejected.** That's Approach E. The body has subtle pieces (`<SUBAGENT-STOP>`, Instruction Priority, platform notes) that the model needs. 3 KB is cheap.

Considered: ship without smoke test (T2.6); call it done after unit tests pass.

**Rejected.** The whole point is behavioural — the smoke test is the only verification that the rule actually takes effect. Without it, we ship green CI and an agent that ignores its own standing instructions.

### 4.5 — What will I wish I'd done differently in the retro?

1. **"I should have spiked the parser first."** → T1.1 IS the spike. Mitigated.
2. **"I should have confirmed the prompt-builder location before estimating."** → T2.1 grep step. Mitigated.
3. **"The smoke test was inconclusive — model behaviour is non-deterministic."** → Run the smoke test 3 times with the same prompt; document the result rate. If 2/3 invoke `writing-plans` (vs. baseline 0/3), declare the rule operative.
4. **"We added always_on and other authors immediately overused it, ballooning prompts."** → 16 KB cap + the policy that ONLY `using-superpowers` opts in at v1. Future opt-ins require their own PR + review.
5. **"Prompt cache invalidation broke a user's billing flow."** → Cache invalidation is one-time per provider per profile. Worst case: 1 cache-miss turn. Acceptable; release notes flag it.

All five folded into the plan above.

### 4.6 — Revised plan summary

The plan that ships, after the audit:

1. **M1:** Add `always_on: bool = False` to skill frontmatter; enforce 16 KB body cap; tests for parser + cap. **1 day.**
2. **M2 (MVP):** New Slot 4b in `base.j2` rendering bodies of `always_on` skills in alphabetical order; flip `using-superpowers/SKILL.md` to `always_on: true`; manual smoke test confirms the 1%-rule takes effect. **1.5 days.**
3. **M3:** Composability tests + plugin-author docs. **0.5-1 day.**
4. **M4:** CHANGELOG entry + cross-doc update. **1 hour.**

**Calendar: ~3 days** for one engineer.

**Plan-B exists** if M1 is harder than expected: Approach E (lift 6 lines into Slot 2). Half-day delivery, partial coverage.

### 4.7 — Pre-flight checklist before any code

- [ ] `pytest opencomputer/agent` is green on `main`.
- [ ] `ruff check opencomputer/agent/ opencomputer/skills/` is clean.
- [ ] Confirm `oc chat` works end-to-end (smoke test in T2.6 depends on it).
- [ ] Confirm parent handoff doc (`2026-05-16-SESSION-HANDOFF.md`) doesn't list this as already-shipped (it doesn't; flagged as open follow-up).
- [ ] Confirm parity plan + awareness cleanup plan have NOT started yet — if they're in flight, this plan is post-them (per the standard ordering).

If any of these fail, halt and report; don't paper over.

---

## Honest closing note

This plan ships an architectural improvement on top of claude-code's hardcoded handling of `using-superpowers`. Claude Code special-cases that one skill name; OC generalises via a frontmatter flag, paying ~1 extra day for it. That's the right trade — OC's plugin authors will eventually want their own always-on skills (failure-recovery-ladder, brainstorming, requesting-code-review are plausible candidates), and shipping the general primitive first is cheaper than retrofitting later.

The most important thing this plan acknowledges is that **landing the code without the smoke test is incomplete**. Unit tests can verify the body renders into the prompt; only a real-model smoke test can verify the model now obeys the standing instruction. If the smoke test fails (model still ignores skills), the bug is in the skill body text or model alignment, not in this code — and that's a separate follow-up the renderer can't solve.
