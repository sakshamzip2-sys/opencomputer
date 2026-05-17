# OC skill-frontmatter baseline (M1.T1.1)

Date: 2026-05-17
Owner: always-on skill-injection PR
Source files:
- `opencomputer/agent/memory.py:245-413` — dataclasses
- `opencomputer/agent/memory.py:419-579` — frontmatter parser helpers
- `opencomputer/agent/memory.py:1244-1379` — `MemoryManager.list_skills`
- `tests/test_skill_frontmatter_extra_fields.py` — parser unit tests

## What is parsed today

A `SKILL.md` frontmatter (YAML between `---` fences) is read by `frontmatter.load(...)` and exposed as `post.metadata: dict`. `MemoryManager.list_skills()` walks user + bundled + hub roots, opens each `SKILL.md`, and constructs a `SkillMeta` from the metadata. The constructor call at `memory.py:1336` reads exactly these keys:

| Frontmatter key | SkillMeta field | Source |
|---|---|---|
| `name` | `name` | inline at construction |
| `description` | `description` | inline |
| `version` | `version` | inline (default `"0.1.0"`) |
| `priority` | `priority` (float \| None) | inline, tolerant to malformed input |
| `required_environment_variables` | `required_env_vars` | `_parse_required_env_vars` (P3.4) |
| `required_credential_files` | `required_credential_files` | `_parse_required_credential_files` (P3.5) |
| `requires` | `requires` (binaries/env/os/plugins) | `_parse_skill_requires` (OpenClaw) |
| — | `unmet_requirements` | computed via `_evaluate_skill_requirements` |

Plus `references` + `examples` loaded from sibling dirs via `_load_references_dir`.

## What is documented BUT NOT wired (pre-existing gap)

`_parse_skill_extras(raw)` at `memory.py:504-579` parses CC §7 fields:

| Field | Default | Documented at |
|---|---|---|
| `disable_model_invocation` | `False` | line 388-392 |
| `user_invocable` | `True` | line 393-396 |
| `argument_hint` | `""` | line 397-399 |
| `paths` | `()` | line 400-404 |
| `model` → `skill_model` | `""` | line 405-408 |
| `allowed_tools` | `()` | line 409-413 |

The dataclass HAS these fields. The parser HAS them parsed. **Twenty-two parser-level unit tests pass.** But `list_skills` never calls the parser — the `SkillMeta(...)` constructor at line 1336 omits the extras, so the dataclass defaults always win.

Empirical confirmation (test SKILL.md with `paths: [never-matches]`, `disable_model_invocation: true`, `user_invocable: false` → loader returns `paths=(), disable_model_invocation=False, user_invocable=True`).

**Impact on the always-on plan:** plan T3.1 ("always_on=true + paths=['/no/match'] → body NOT injected") cannot pass without first wiring `_parse_skill_extras` into `list_skills`. The fix is one line (call the parser, splat the result into the constructor).

## API stability bar

`SkillMeta` is `@dataclass(frozen=True, slots=True)`. New fields:
- Must default to a value (positional kwargs after `path: Path` are required-without-default if defaults are missed).
- Adding a field is non-breaking for callers using `SkillMeta(id=..., name=..., description=..., path=...)`-shape construction (kwargs-only).
- Direct positional construction would break — grep confirms ALL call sites use kwargs (skills_hub/sources/*, list_skills loader, tests). Safe.

## Parser tolerance posture

Existing parsers are **uniformly permissive**: any malformed value silently falls back to the default. The plan's 16 KB body cap should keep the same shape — oversized body → `always_on=True` flips to `False` with a WARN log, but the skill still loads. Never raise during `list_skills`; never block sibling skills.

## Decision: gate continuation

T1.1 was a gate per plan ("If parser is strict-schema, fall back to … or escalate"). **The parser is fully permissive and additive-tolerant** — `always_on: bool = False` slots in cleanly. Proceed with M1.T1.2.

The pre-existing extras-wiring gap is acknowledged and will be closed in T1.2 as a one-line additive change (call `_parse_skill_extras(meta.get('extras', meta))` and splat). This is in-scope-by-necessity for T3.1 composability tests.
