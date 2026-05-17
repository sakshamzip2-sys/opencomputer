# Retro — using-superpowers always-on injection

Date: 2026-05-17
Plan: [`PLAN.md`](./PLAN.md)
Status: [`STATUS.md`](./STATUS.md)

## Built (one paragraph)

A new `always_on: true` skill-frontmatter flag now causes a skill's body to be rendered into every system prompt via Slot 4b of `opencomputer/agent/prompts/base.j2`. The bundled `using-superpowers` skill opts in, so the model sees the 1%-rule, `<SUBAGENT-STOP>` guard, and Instruction-Priority body unconditionally instead of having to first invoke the Skill tool. The schema lives on `SkillMeta.always_on: bool`; the loader enforces a 16 KB body cap with a `WARN`-on-violation that flips the flag back off; the renderer applies `paths` gating defensively + sorts opt-in bodies alphabetically for prompt-cache stability. A latent parser-wiring bug (`_parse_skill_extras` was orphaned from `list_skills` — CC §7 fields silently always-defaulted in production) was fixed alongside because composability test T3.1 required it. 33 new tests; 1,711 tests across the touched surface green; ruff clean.

## 🎉 Went well

1. **Plan was honest about its own risk.** Phase 4.7 pre-flight + 4.5 "what would I regret in retro" let me catch the latent `_parse_skill_extras` bug at T1.1 (the baseline-doc step), not in T3.1 when tests would have inexplicably failed. The plan's "spike the parser first" instruction paid off literally.
2. **TDD cycle on the schema/parser was clean.** RED (15 failing tests) → minimal GREEN (TypedDict + field + parser + loader wiring) → no refactor needed. The TypedDict approach also gave free type-narrowing for the `**extras` splat — no `cast()` or `# type: ignore` lying around.
3. **Slot 4b round-tripping just worked.** Jinja `{{ body }}` value-substitution is one-level, so bodies with raw `{{`, `{%`, `<EXTREMELY-IMPORTANT>` round-trip verbatim with zero escaping games. The autoescape-disabled-for-`.j2` setting was already in place; one less knob to think about.
4. **Composability matrix tests caught the second wiring gap.** Wrote `test_always_on_with_non_matching_paths_not_injected` expecting it to pass; it failed, exposing that `skill_matches_cwd` has zero production callers. Defense-in-depth fix at the renderer (one new kwarg + one `if` check) closed the loop without expanding scope.
5. **The pre-existing extras-parsing fix landed alongside on merit.** Not a scope creep — the composability test couldn't pass without it. Documented honestly in STATUS.md and CHANGELOG as "in-scope-by-necessity," not hidden.

## 😤 Was hard

1. **The smoke test (T2.6) didn't show behavioural change.** Three `oc chat` runs against `claude-router`; wire confirmed (body in prompt at byte 49983 of 60329), but the model answered without invoking the `Skill` tool. Per the plan's §4.5 prediction, this is a body-text/model-alignment concern, not a renderer bug — but it's a real outcome that needs follow-up.
2. **`oc` binary vs. `python -m opencomputer` resolution.** First smoke ran against the parent's code (uv-tools install pointed at `/Users/saksham/Vscode/claude/OpenComputer/`), not the worktree. Had to `uv tool install --force --reinstall --editable .` from the worktree to get smoke 2-3 to actually exercise the new code. CLAUDE.md mentions `pip install -e .` for the worktree's venv but not the uv-tools binary path — worth noting.
3. **`skill_matches_cwd` walk-up semantics broke first composability test.** Patterns like `**/*.go` glob from the cwd up to filesystem root, so a `tmp_path/docs-only` cwd matched because some ancestor (e.g. the OpenComputer repo itself) had a `.go` file. Fix: anchor patterns with a unique-prefix dirname. Pre-existing quirk in the matcher, but test-author surprise.
4. **Output-file truncation hid the second smoke's actual error.** `oc chat -q ... --auto` failed at `_run_oneshot_turn` (asyncio.run from a running loop), but the rich-formatted traceback in the captured background-task output didn't flush the exception summary. Had to rerun after `uv tool install` instead of digging.

## 🔄 Next time

1. **Whenever extending `SkillMeta`, also grep for "is the parser wired in?"** The CC §7 wiring gap had been silently dormant for who knows how many weeks. Pattern: parser exists + parser unit tests pass + integration site never calls parser = bug. Add an integration test for every new SkillMeta field that does the full SKILL.md → MemoryManager.list_skills round-trip.
2. **For worktree-based development, do `uv tool install --force --reinstall --editable .` immediately after `pip install -e .`** instead of waiting for the smoke test to surprise you. Update CLAUDE.md §5 ("Worktree / merge refresh — non-negotiable") to include the uv-tools step.
3. **For Phase 7 reviews, run an independent code-reviewer subagent in parallel with my own self-review.** I started this one too late (during the review phase rather than at the start of phase 7); the subagent has the strongest signal when it sees the work fresh.
4. **For composability tests involving `skill_matches_cwd`, use unique-prefix dirnames** (`uniqueprojectroot_xyz/**`) to keep the walk-up matcher from spuriously matching ancestor dirs. Document this hint in `tests/test_skill_frontmatter_extra_fields.py` or wherever the pattern is most discoverable.
5. **For behaviour-dependent features (like `always_on`), the wire-verification test matters more than the model-behaviour test.** Wire test: direct-prompt inspection asserting `"# Standing skill instructions" in prompt`. Model test: best-effort, document the run count and outcome rate, don't gate on it.

## 📚 Learned

- **TypedDict + `**`-splat narrows types across a dict boundary.** Useful pattern for any "parse YAML into structured args" call site. The TypedDict additionally enforces compile-time consistency — adding a SkillMeta field that's missing from the TypedDict surfaces immediately.
- **Jinja value substitution is one-level.** `{{ body }}` outputs the body string literally; the body is never re-evaluated as a template. So markdown content with `{` / `{{` / `<TAG>` round-trips without `| safe` or escape filters. (Caveat: this assumes autoescape is OFF, which `.j2` extension is by virtue of `select_autoescape(disabled_extensions=("j2",))`.)
- **`skill_matches_cwd` walks up to filesystem root.** Cwd-gating glob patterns need to be anchored to a unique dirname to avoid spurious matches against ancestor dirs. Tests should reflect this.
- **`_collect_always_on_bodies` is a small file-read on every prompt build.** Acceptable cost for now (file is ~3 KB, prompt-builds are 1-2x per turn). If always_on grows beyond a handful of skills, cache the (path → mtime → body) map.

## 📋 Open items / tech debt

| Item | Severity | Path |
|---|---|---|
| Model doesn't auto-invoke `Skill` tool even with Slot 4b body in prompt | 🟡 medium | Body-text rework, model-alignment investigation, or both |
| `skill_matches_cwd` has zero production callers outside Slot 4b — agent loop should filter by paths before passing skills to PromptBuilder | 🟡 medium | `opencomputer/agent/loop.py` — separate PR |
| `extensions/.../skills_hub/sources/*.py` construct `SkillMeta` directly without `_parse_skill_extras` — `always_on` / `paths` / etc. from frontmatter are ignored for those skill sources | 🟡 medium | Each source loader needs to call `_parse_skill_extras` |
| Body file is re-read on every prompt build (acceptable today, may matter later) | 🟢 low | Cache by (path, mtime) if always-on grows |
| Smoke test infra: `oc chat -q` rich-traceback truncation in background-task captures hid actual errors | 🟢 low | Either dump rich-disabled in `--quiet`, or capture stderr separately |
| Renderer-side body-cap check is defense-in-depth duplicate of loader-side cap. Belt-and-braces correct, but could become inconsistent if cap value drifts. Keep them in lockstep via `ALWAYS_ON_BODY_CAP_BYTES` import. (Already done.) | 🟢 low | Maintenance note only |

## File list

See [`STATUS.md`](./STATUS.md) "Files touched" table.
