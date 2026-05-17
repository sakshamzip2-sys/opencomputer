# Skill Authoring Guide

OpenComputer skills follow the [Anthropic Agent Skills spec](https://docs.claude.com/en/agents-and-tools/agent-skills/best-practices) with two intentional divergences (documented inline below).

## Frontmatter rules

### `name`
- Lowercase letters + digits + hyphens only.
- ≤50 characters.
- **Gerund form preferred:** `processing-pdfs`, not `pdf-helper`. `analyzing-spreadsheets`, not `excel-utility`.
- **Forbidden:** any token equal to `anthropic` or `claude` (case-insensitive).

### `description`
- ≤280 characters. (OpenComputer caps this stricter than Anthropic's 1024 because routing degrades on long descriptions.)
- **Third-person voice.** Describe what the skill does, like a system describing itself.
  - Good: `Processes PDF files and extracts text. Use when working with PDFs.`
  - Bad: `I can help you extract text from PDFs.`
  - Bad: `You can use this to extract text.`
- **Must include both WHAT and WHEN.**
  - Pattern: `<3rd-person verb phrase>. Use when <trigger condition>.`
- No XML/HTML tags.

### Optional fields
- `version`: semver string (e.g. `0.1.0`).
- `size_review_date`: ISO date (e.g. `2026-05-02`). Documents an intentional exemption from the body-size warning. Use when a skill genuinely earns its >500-line size.
- `priority`: float; higher values surface earlier in the skill list. Default `null` = alphabetical fallback.
- `requires`: OpenClaw-parity host-capability gate. Map of `binaries` / `env` / `os` / `plugins` lists; the skill loads but is flagged with `unmet_requirements` when any item is missing.
- `required_environment_variables` / `required_credential_files`: Hermes-parity passthrough hints — make env vars / credential files visible to ExecuteCode + the Docker sandbox.

### CC §7 fields (parsed by the loader)
Both snake_case and dashed (`always-on`, `disable-model-invocation`, …) keys work; the loader accepts both Python and Claude-Code conventions.

- `disable_model_invocation` (bool; default `false`): only humans can invoke this skill via `/<name>`; the LLM cannot auto-call it. Use for destructive operations (`deploy`, `commit`) where you want a manual gate even when the agent could pick the skill.
- `user_invocable` (bool; default `true`): set to `false` to hide the skill from the `/`-autocomplete menu. Body content can still be injected via `always_on`; only the slash discovery surface is hidden.
- `argument_hint` (string; default `""`): free-text hint shown next to the slash name in autocomplete (e.g. `"<file_path>"`).
- `paths` (list[str] or scalar string; default `[]`): cwd-gating glob array. When non-empty, the skill only auto-activates in directories whose cwd-or-ancestor matches at least one pattern (see [`skill_matches_cwd`](../../opencomputer/agent/memory.py)). Empty = universal.
- `model` (string; default `""`): per-skill model override. Mirrors `AgentTemplate.model`.
- `allowed_tools` (list[str]; default `[]`): per-skill tool allowlist. When non-empty, only these tools are visible to the model while the skill is the active context.
- `always_on` (bool; default `false`): the skill body is auto-injected into every system prompt (rendered by Slot 4b of `base.j2`). The model sees the standing rules on turn 0 without needing to invoke the Skill tool first. Bodies are capped at 16 KB — oversize bodies silently flip the flag back to `false` with a `WARN` log. Use sparingly; every opt-in adds prompt-token cost to every turn across every session.

#### `always_on` composability matrix

| Combined with | Result |
|---|---|
| `paths: [...]` (non-matching cwd) | Body NOT injected — `paths` wins. The author opted into cwd-gated activation; honor it. |
| `paths: [...]` (matching cwd) | Body IS injected. |
| `disable_model_invocation: true` | Body IS injected; the model just can't invoke the skill via the tool surface. The two fields are orthogonal — body provides knowledge, invocation flag controls triggering. |
| `user_invocable: false` | Body IS injected; only the slash menu hides the skill. |
| `context: fork` | Orthogonal — `context` affects invocation lifetime, not prompt presence. Body injects normally. |

#### When to use `always_on`

Reserved for **discipline-forcing rules** that must reach the model unconditionally — typically meta-skills about how to use other skills (e.g. `using-superpowers`). Avoid for:
- Per-task playbooks (use `paths:` instead).
- Anything model-invocable that the agent should choose to use based on relevance — let it discover via the description.
- Anything over ~8 KB of body — even within the 16 KB cap, every always-on byte multiplies by every turn × every session.

When in doubt, ship without `always_on` and add it later if the model demonstrably misses the skill it should be calling.

## Body rules

- Top-level `# Title` heading matches the slug in Title Case.
- ≤500 lines for optimal performance. Split larger skills into reference files.
- Forward slashes only in paths (`reference/foo.md`, not `reference\foo.md`).
- No time-sensitive content (`after August 2025`, `before next quarter`). Use a collapsible "Old patterns" section if you must reference deprecated approaches.

## Reference files

- Place under `<skill>/references/` or `<skill>/examples/`.
- ≤1 level deep from SKILL.md (no SKILL → A → B chains; Anthropic-skill loaders do partial reads on nested files and miss content).
- Files >100 lines must have a TOC at the top.

## Worked examples

### Good

```yaml
---
name: writing-commit-messages
description: Synthesizes conventional-commit messages from staged diffs. Use when the user asks for help writing or improving a git commit message.
version: 0.2.0
---

# Writing Commit Messages

## When to use
- The user asks for a commit message.
- The user has staged changes and asks for help.
- The user wants to improve an existing commit message.

## Steps
1. Run `git diff --staged` to read the changes.
2. Identify the type (feat, fix, refactor, docs, chore, test).
3. Identify the scope (subsystem touched).
4. Write a one-line subject ≤72 chars.
5. If non-trivial, add a 2-3 sentence body.
```

### Bad (rewritten to good)

Original (bad):
```yaml
---
name: pdf-helper
description: I can help you with PDFs! Just tell me what you want to do.
---
```

Fixed (good):
```yaml
---
name: processing-pdfs
description: Processes PDF files — extracts text, fills forms, merges documents. Use when working with PDF files or when the user mentions PDFs, forms, or document extraction.
version: 0.1.0
---
```

What changed:
- `pdf-helper` (noun) → `processing-pdfs` (gerund).
- 1st-person ("I can help you") → 3rd-person ("Processes").
- Vague ("Just tell me what you want") → specific WHAT + WHEN.

## Validation

Before committing a skill, run:

```bash
pytest tests/skills_hub/test_bundled_corpus_compliance.py -k "<your-skill-slug>" -v
```

This runs the unified validator in lenient mode. Hard errors (reserved words, XML, malformed frontmatter) block the commit; warnings are advisory.

For new skills (not yet in the bundled corpus), use strict mode:

```python
from opencomputer.skills_hub.agentskills_validator import validate_skill_dir
report = validate_skill_dir(Path("path/to/your-skill"))
report.raise_if_errors()
```
