# Using-superpowers always-on injection — STATUS

Date: 2026-05-17
Branch: `worktree-always-on-skill-injection-2026-05-17`
Plan: [`PLAN.md`](./PLAN.md)
CHANGELOG: see entry at top of `[Unreleased]` in repo-root `CHANGELOG.md`

## Shipped (M1 + M2 + M3 + M4)

| Milestone | Status | Tests |
|---|---|---|
| M1 — Schema + parser + body cap | ✅ shipped | 15 new (`tests/test_skill_always_on.py`) |
| M2 — Renderer Slot 4b + flip `using-superpowers` ON | ✅ shipped (MVP) | 9 new (`tests/test_prompt_slot_4b_always_on.py`) |
| M3 — Composability tests + plugin docs + example | ✅ shipped | 9 new (`tests/test_skill_always_on_composability.py`) |
| M4 — CHANGELOG + handoff cross-link | ✅ shipped | n/a |

Total: **33 new tests**, all green. Full-suite regression on `skill` / `memory` / `prompt` test families: **1,033 passed, 6 skipped, 0 failed**.

## Behavioural smoke (T2.6)

3 runs against `claude-router` proxy, `oc chat -q ... --auto`.

| Run | Prompt | Skill invoked? | Notes |
|---|---|---|---|
| 1 | "Please make a plan for adding a logout button to my web app" | ❌ | Model asked clarifying questions instead |
| 2 | "How would I implement caching in our Python REST API? Walk me through the design." | ❌ | Model produced a structured plan inline — what `writing-plans` would have produced, just without invoking the tool |
| 3 | "I want to brainstorm a new feature for our authentication system. Help me explore the design space first before we code anything." | ❌ | Model asked clarifying questions, no `Skill` invocation |

**Wire verified.** Direct-prompt inspection (`PromptBuilder.build(skills=mm.list_skills())`) confirms Slot 4b appears at byte 49983 of the 60329-byte system prompt; `1% rule`, `<SUBAGENT-STOP>`, and the `digraph` braces all round-trip cleanly. The model is receiving the standing instruction; it's just not honoring it as an unconditional auto-invoke.

**Diagnosis** (per the plan's own §4.5 prediction): this is a body-text / model-alignment concern, not a renderer bug. The renderer can't make the model honor the rule any harder than the model already weighs other context. Options for follow-up (not in this PR):

1. **Sharpen the body**: the current body says "you MUST invoke skills" but also lists rationalizations to avoid — those rationalizations may compete for attention with the imperative. Tightening could help.
2. **Slot positioning**: Slot 4b is at position ~50K of a ~60K prompt. Earlier placement (e.g. Slot 1b right after SOUL.md) may produce stronger adherence at the cost of breaking the historical slot ordering invariants.
3. **Tool catalog naming**: the body says "invoke `writing-plans`" but the actual tool surface uses `Skill(name="writing-plans")`. Some models may not bridge that. Renaming the body to use the actual tool call shape may help.
4. **Try a non-proxy provider**: the smoke ran via `claude-router`. A direct Anthropic call may behave differently.

These are documented for the next session to pick up; the renderer + schema are done.

## In-scope-by-necessity fixes shipped alongside

`_parse_skill_extras` was unwired in `MemoryManager.list_skills` (defined + unit-tested at the parser level, never called from the loader). All six CC §7 fields (`disable_model_invocation`, `user_invocable`, `argument_hint`, `paths`, `model`, `allowed_tools`) silently defaulted in production. This PR wires the parser in (one-line addition + `**extras` splat) because composability test T3.1 (`always_on` + `paths` gating) requires `paths` to actually fire from frontmatter.

Empirical proof of the gap (pre-fix): a SKILL.md with `paths: [never-matches]` + `disable_model_invocation: true` + `user_invocable: false` returned `paths=(), disable_model_invocation=False, user_invocable=True` from `list_skills`.

**Important honest framing** (per code-reviewer feedback): this PR only closes the **data-flow** gap. The fields are now correctly populated on `SkillMeta` instances, BUT downstream consumption is still partial:

- `paths` is now consumed by Slot 4b (this PR) and via the existing `skill_matches_cwd` helper (which still has no production caller in the agent loop — see "Out of scope" below).
- `disable_model_invocation` is parsed and stored, but no production code reads it. The `Skill` tool's invocation dispatcher does not check the flag yet — a follow-up PR touching `opencomputer/tools/skill.py` is needed to actually block model auto-invocation.
- `user_invocable` is parsed and stored, but the slash-command discovery surface does not filter by it yet. Skills with `user_invocable: false` still show up in the `/`-autocomplete menu today.
- `argument_hint`, `model`, `allowed_tools` similarly: parsed-and-stored only.

So the wire is now correct for the field this PR introduces (`always_on`, Slot 4b), and CORRECT for `paths` along the Slot 4b code path specifically. The other CC §7 fields' enforcement is unblocked but still pending follow-up.

## Out of scope / deferred

- **`skill_matches_cwd` integration in the agent loop.** Slot 4b applies cwd-gating defensively at the renderer layer (correct for prompt injection). The agent loop itself doesn't call `skill_matches_cwd` to filter the skill *list* either — meaning a paths-restricted skill still shows up in Slot 4 (the menu) regardless of cwd. Fixing that needs a separate PR touching `loop.py`.
- **skills_hub-source-loaded skills don't benefit from this wiring.** `extensions/.../sources/{minimax,github,url,well_known}.py` construct `SkillMeta` directly via the dataclass kwargs path; they bypass `list_skills` and thus skip `_parse_skill_extras`. Those source loaders would need their own extras-parsing call to honor `always_on` / `paths` / etc.
- **`disable_model_invocation` / `user_invocable` / `argument_hint` / `skill_model` / `allowed_tools` consumers.** The fields are now populated from frontmatter, but downstream enforcement is still pending: the `Skill` tool's dispatch path (`opencomputer/tools/skill.py`) needs to consult `disable_model_invocation` before auto-invoking; the slash discovery surface needs to filter on `user_invocable`; the autocomplete UI needs to surface `argument_hint`; etc. Each enforcement site is a small, well-scoped follow-up PR.
- **Behavioural smoke pass.** See above — the wire is verified; model adherence is the follow-up.

## Files touched

| Path | Type | Note |
|---|---|---|
| `opencomputer/agent/memory.py` | edited | `ALWAYS_ON_BODY_CAP_BYTES` constant + `SkillMeta.always_on` field + `_SkillExtras` TypedDict + `always_on` in `_parse_skill_extras` + `**extras` splat in `list_skills` + body-cap check |
| `opencomputer/agent/prompt_builder.py` | edited | `_collect_always_on_bodies` helper + paths-gating defense + render-time cap check + `always_on_skills` template var |
| `opencomputer/agent/prompts/base.j2` | edited | New Slot 4b after Slot 4 (between Slot 4 and Slot 5) |
| `opencomputer/skills/using-superpowers/SKILL.md` | edited | Frontmatter gains `always_on: true` |
| `tests/test_skill_always_on.py` | new | 15 tests — schema + parser + cap + loader wiring |
| `tests/test_prompt_slot_4b_always_on.py` | new | 9 tests — renderer + Jinja round-trip + frontmatter strip |
| `tests/test_skill_always_on_composability.py` | new | 9 tests — paths + fork + disable + user_invocable + defense-in-depth cap |
| `examples/example-always-on-skill/SKILL.md` | new | Copy-paste starter for skill authors |
| `docs/skills/AUTHORING.md` | edited | New "CC §7 fields" section + always_on composability matrix |
| `docs/refs/oc-skill-frontmatter-baseline.md` | new | The pre-change baseline doc (gap-discovery artifact) |
| `docs/superpowers/specs/2026-05-16-using-superpowers-injection/STATUS.md` | new | This file |
| `docs/superpowers/specs/2026-05-16-SESSION-HANDOFF.md` | edited | ADDENDUM cross-link added at end |
| `CHANGELOG.md` | edited | New `[Unreleased]` entry at top |
