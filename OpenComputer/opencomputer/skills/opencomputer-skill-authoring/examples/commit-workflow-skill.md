# Example — a full "conventional commits" skill

This is a complete sample skill showing the frontmatter + body layout
plus one `references/` file. Drop it under any skill root
(`opencomputer/skills/`, a plugin's `skills/`, or
`~/.opencomputer/<profile>/home/skills/`) and it will be picked up at
discovery.

## File: `conventional-commits/SKILL.md`

```markdown
---
name: Conventional Commits
description: Use when the user is about to commit, asks to "write a commit message", mentions "conventional commits", or needs help picking a commit type (feat, fix, chore, refactor, docs, test, build, ci, perf, style).
version: 0.1.0
---

# Conventional Commits

When the user is writing a commit, apply Conventional Commits v1.0.0 to
produce a message that machines and humans can both read.

## 1. Shape

```
<type>(<scope>): <short summary>

<optional body — wraps at 72>

<optional footer: BREAKING CHANGE: ...  OR  Refs: #123>
```

## 2. Pick the type

- `feat` — new user-facing feature.
- `fix` — bug fix the user can observe.
- `refactor` — internal change, no behavior change.
- `perf` — performance improvement.
- `test` — adds or fixes tests only.
- `docs` — docs only.
- `style` — formatting / whitespace / linting, no code change.
- `build` — build system or dependency change.
- `ci` — CI config or pipeline change.
- `chore` — everything else (version bumps, config tweaks).

When in doubt between `refactor` and `fix`, ask: did the user's
behavior change? Yes → `fix`. No → `refactor`.

## 3. Scope (optional)

One or two words naming the subsystem. Lowercase. Omit if the change
is repo-wide or doesn't belong to one area.

## 4. Summary line

- Lowercase first letter.
- No trailing period.
- <=72 chars including the `<type>(<scope>):` prefix.
- Imperative mood: "add parser" not "added parser".

## 5. When to write a body

Write a body when:

- The change has non-obvious motivation (explain WHY).
- It touches multiple files for one logical reason (group them).
- It carries a footnote the reviewer needs (perf regression, known
  issue, linked decision doc).

Skip the body for small obvious changes.

## 6. Breaking changes

Append `!` to the type: `feat!: drop legacy X support`. Add a
`BREAKING CHANGE:` footer explaining the migration.

## 7. Pre-commit gates

If the repo has commit hooks, run them first. See
[references/commit-hooks.md](references/commit-hooks.md) for the
common ones.
```

## File: `conventional-commits/references/commit-hooks.md`

```markdown
# Commit hook integration for Conventional Commits

Several commit-hook tools enforce the Conventional Commits shape
automatically. Know which one the repo uses before you format your
message.

## commitlint

Look for `.commitlintrc.json` / `.commitlintrc.yaml` at the repo root
or a `commitlint` block in `package.json`. Runs via `husky` at commit
time. If it rejects a message, the reason is printed to stderr — read
it before re-writing.

## commitizen (`cz`)

A Python or JS CLI that prompts you through the shape interactively.
Look for `.cz.yaml` or a `[tool.commitizen]` block in `pyproject.toml`.
Running `cz commit` substitutes for `git commit` and builds the
message from prompts.

## gitlint

Python-only lint, runs via `gitlint-cli` or a pre-commit hook. Config
at `.gitlint`. Catches line length, trailing punctuation, imperative
mood issues.

## How to detect which one is active

```bash
# Look for config files
ls .commitlintrc* .cz.yaml .gitlint 2>/dev/null
grep -l commitizen pyproject.toml 2>/dev/null
grep -l commitlint package.json 2>/dev/null

# Look at recent commits for style clues
git log --oneline -20
```

If none of these exist, the repo has no enforcement — apply the shape
yourself from the main SKILL.md.

## Hooking into OpenComputer

If you want OpenComputer itself to enforce commit style on every
commit, add a `PreToolUse` hook matching `Bash` with a command-line
inspection. See the `opencomputer-hook-authoring` skill for the
pattern.
```

## Directory layout

After dropping both files, the tree looks like:

```
conventional-commits/
├── SKILL.md
└── references/
    └── commit-hooks.md
```

You could also add `examples/` with a sample commit message for each
type, but for this skill the SKILL.md body is concrete enough.

## How discovery picks it up

When `MemoryManager.list_skills()` runs:

1. It walks each skill root (user, bundled).
2. It finds `conventional-commits/SKILL.md` and reads its frontmatter.
3. It enumerates `conventional-commits/references/*.md` — picks up
   `commit-hooks.md`.
4. It enumerates `conventional-commits/examples/*` — empty, so no
   examples are attached.
5. It creates a `SkillMeta` with `name="Conventional Commits"`,
   `description=<full trigger query>`, and one reference.

At retrieval time the agent sees the description and the body; it can
load the reference on demand when a question about hook integration
comes up.
