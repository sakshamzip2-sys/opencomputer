# Bundled Corpus Compliance Audit — 2026-05-02

Initial state: 127 bundled skills under `opencomputer/skills/`. Audit triggered by adoption of Anthropic's published Agent Skills spec.

## Hard errors found and fixed

13 hard errors across 3 categories were repaired during Group C of the SP1 implementation.

### Category A — reserved-word `name:` (1 skill)

| Skill | Original `name` | Fix |
|---|---|---|
| `claude-code` | `claude-code` | Narrowed validator (the reserved-word check now ignores the directory slug and only inspects the parsed `name:` field), then renamed `name:` to `using-claude-code` to match the gerund convention. |

### Category B — Title Case names (8 skills)

`name:` field used Title Case or mixed case but the directory slug was already kebab-case lowercase. All 8 were lowercased + kebabbed to match the directory.

### Category C — over-500-char descriptions (4 skills)

Trimmed to ≤500 chars (and below the OpenComputer 280-char routing cap where possible).

| Skill | Before | After |
|---|---:|---:|
| architecture-diagram | 791 | 471 |
| powerpoint | 688 | 441 |
| p5js | 670 | 463 |
| ascii-video | 587 | 477 |

## Body-size violations (11 skills > 500 lines)

### Split into reference files (3 skills, Group E)

| Skill | Original lines | Final SKILL.md lines | Reference files added |
|---|---:|---:|---|
| research-paper-writing | 2,375 | 215 | 13 reference files (`phase-0-discovery`, `phase-1-…` through `phase-8-publication`, `iterative-refinement`, `workshop-and-short-papers`, `paper-types-non-empirical`, `hermes-agent-integration`, plus supporting reference docs) |
| using-claude-code (renamed from `claude-code`) | 744 | 285 | 3 reference files (`cli-reference`, `interactive-session`, `advanced-integration`) |
| hermes-agent | 705 | 186 | 5 reference files (`cli-reference`, `slash-commands`, `key-paths-and-config`, `spawning-instances`, `contributor-reference`) |

### Exempted via `size_review_date: 2026-05-02` (8 skills, Group F)

These skills are domain-API references where size reflects API-surface density. Splitting would harm discoverability.

| Skill | Lines | Rationale |
|---|---:|---|
| audiocraft | 567 | Single-library reference. |
| coding-standards | 523 | Single-domain reference; sections are interdependent. |
| dspy | 593 | Single-API reference. |
| github-repo-management | 515 | Single-domain reference. |
| outlines | 655 | Single-domain library reference. |
| p5js | 547 | Single-library reference. |
| weights-and-biases | 593 | Single-API reference; sections are interdependent. |
| writing-skills | 655 | Meta-skill about skill authoring; cross-references would multiply confusion. |

> **Note on count drift from spec §5.5.** The design doc (spec §5.5) projected 11 exempt skills based on a pre-audit line-count survey. The actual exempt set is 8 because three of the originally-projected skills (`llm-wiki`, `prp-plan`, `segment-anything`) had already been suppressed from the >500-line list by other Group E/preflight passes — either via prior splits or because their measured line count fell below threshold once frontmatter normalization landed. The 8 above are the real, observed set after Groups A–E completed.

Re-evaluate at next year's audit (2027-05-02 or earlier if a skill grows past ~750 lines).

## Voice-violation warnings (count: 1)

A single skill emits a `description.voice` warning under `validate_skill_md(strict=False)`:

- `brainstorming`

Cleanup deferred to a follow-up PR. The violation:
- Is logged in the validator output for tracking.
- Does not block ingestion (warning, not error).
- Will be cleaned up when the skill is next touched, or in a dedicated voice-cleanup PR.

## Outcome

- ✅ All 127 bundled skills pass `validate_skill_md(strict=False)` with zero errors.
- ✅ 0 `body.size_warn` warnings remaining (all addressed via split or exemption).
- ⚠️ 1 voice-violation warning (`brainstorming`) — tracked, deferred to follow-up PR.
